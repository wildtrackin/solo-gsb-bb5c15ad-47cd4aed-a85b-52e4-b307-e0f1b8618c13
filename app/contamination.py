"""污染接触溯源。

处理流程：
1. 复用现有单链状态机校验器（``validate_events``）分别校验每条链，原始结果
   原样保留在各链结果的 ``validation`` 字段中（状态机违规不影响审计本身，
   违规事件不产生持有区间）；
2. 仅以各链 ``status=ok`` 的 seal / reseal / handover 事件生成“展品按保管人”
   的半开持有区间 ``[start, end)``：每个有效事件以其时间戳开启对应保管人的
   区间，下一个有效事件闭合前一区间，最后一个区间向右开放并在 ``audit_until``
   处截断；unseal 不改变保管人，违规事件一律不产生区间；
3. 两条*不同*链（展品）在*同一保管人*名下的持有区间存在正长度重叠
   （``max(start) < min(end)``，半开语义）时形成一次接触，接触段为
   ``[contact_start, contact_end)``；
4. 污染级联传播：污染源链自 ``contaminated_at`` 起被污染；沿一次接触 u → v
   传播的最早时刻为 ``max(u.earliest_contaminated_at, contact_start)``，且该
   时刻必须严格落在接触段内（``< contact_end``，接触仍在持续）、不晚于
   ``audit_until``——即“源已污染且接触仍在持续的最早时刻”。以带优先队列的
   Dijkstra 级联求每条链的最早污染时刻与直接来源：先按污染时刻最小，同一
   最早时刻并列时按传播路径的 chain_id 序列字典序（先看源头、再逐跳向后）
   固定唯一结果；
5. 依直接来源回溯完整传播路径（逐跳含保管人、接触段与发生时刻）；
   截至 ``audit_until`` 未被污染到达的链单独按 chain_id 字典序列出。

事件时间戳、污染源 ``contaminated_at`` 与 ``audit_until`` 不得混用带时区与
不带时区的时间；污染源 ``chain_id`` 未知、重复或 ``audit_until`` 早于任一链
最后一个有效事件时间时返回 422。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .models import (
    ContaminationChainResult,
    ContaminationHop,
    ContaminationTraceRequest,
    ContaminationTraceResponse,
    ContaminationTraceSummary,
    Event,
    EventResult,
    EventType,
)
from .timeline import ensure_comparable, ok_event_records
from .validator import validate_events

# 生成保管人持有区间的事件类型：seal / reseal / handover（unseal 不改变保管人）
_CUSTODIAN_TYPES = (EventType.SEAL, EventType.RESEAL, EventType.HANDOVER)


class ContaminationTraceError(ValueError):
    """溯源入参的语义错误（在路由层转换为 HTTP 422）。"""


@dataclass(frozen=True)
class _HoldingInterval:
    """一条链在某保管人名下的半开持有区间 [start, end)；end 在 audit_until 截断。"""

    start: datetime
    end: datetime
    custodian: str
    event_index: int  # 开启该区间的有效事件在该链中的下标（全链，从 0 开始）


@dataclass(frozen=True)
class _ContactEdge:
    """两链在同一保管人名下的一次正长度重叠接触（有向，from 为污染方时使用）。"""

    from_index: int  # 来源链在 chain_id 字典序下的稳定下标
    to_index: int  # 目标链稳定下标
    custodian: str
    contact_start: datetime
    contact_end: datetime
    from_event_index: Optional[int]
    to_event_index: Optional[int]


def _derive_holding_intervals(
    events: List[Event], event_results: List[EventResult], audit_until: datetime
) -> List[_HoldingInterval]:
    """从 status=ok 的 seal/reseal/handover 生成按保管人的半开持有区间。

    与温控审计相同：每个有效事件开启对应保管人的区间，下一个有效事件闭合
    前一区间；区别仅在于最后一个区间向右开放端在此截断到 audit_until
    （audit_until 不早于最后一个有效事件，故截断不会产生退化区间）。
    """
    records = ok_event_records(events, event_results, _CUSTODIAN_TYPES)
    intervals: List[_HoldingInterval] = []
    for i, record in enumerate(records):
        raw_end = records[i + 1].timestamp if i + 1 < len(records) else audit_until
        end = min(raw_end, audit_until)
        intervals.append(
            _HoldingInterval(
                start=record.timestamp,
                end=end,
                custodian=record.custodian,  # type: ignore[arg-type]
                event_index=record.index,
            )
        )
    return intervals


def _build_contact_edges(
    intervals_by_chain: List[List[_HoldingInterval]],
) -> Dict[int, List[_ContactEdge]]:
    """按保管人分组，为不同链间的正长度重叠区间建立有向接触边（双向各一条）。"""
    by_custodian: Dict[str, List[Tuple[int, _HoldingInterval]]] = {}
    for chain_index, intervals in enumerate(intervals_by_chain):
        for interval in intervals:
            by_custodian.setdefault(interval.custodian, []).append(
                (chain_index, interval)
            )

    adjacency: Dict[int, List[_ContactEdge]] = {
        i: [] for i in range(len(intervals_by_chain))
    }
    for entries in by_custodian.values():
        for i in range(len(entries)):
            chain_i, interval_i = entries[i]
            for j in range(i + 1, len(entries)):
                chain_j, interval_j = entries[j]
                if chain_i == chain_j:
                    # 同一条链在同一保管人名下的区间互不重叠（状态机时间严格递增）
                    continue
                contact_start = max(interval_i.start, interval_j.start)
                contact_end = min(interval_i.end, interval_j.end)
                if not contact_start < contact_end:
                    continue  # 半开区间：仅相接（start == end）不构成接触
                adjacency[chain_i].append(
                    _ContactEdge(
                        from_index=chain_i,
                        to_index=chain_j,
                        custodian=interval_i.custodian,
                        contact_start=contact_start,
                        contact_end=contact_end,
                        from_event_index=interval_i.event_index,
                        to_event_index=interval_j.event_index,
                    )
                )
                adjacency[chain_j].append(
                    _ContactEdge(
                        from_index=chain_j,
                        to_index=chain_i,
                        custodian=interval_i.custodian,
                        contact_start=contact_start,
                        contact_end=contact_end,
                        from_event_index=interval_j.event_index,
                        to_event_index=interval_i.event_index,
                    )
                )
    # 同一来源链的多条接触边按接触起点（再按目标链）稳定排序：
    # 同一路径序列可经由不同接触段同刻到达同一目标时，先处理最早接触段，
    # 使最终选中的 via 边确定（与输入顺序无关）
    for chain_index, edges in adjacency.items():
        edges.sort(key=lambda edge: (edge.contact_start, edge.to_index))
    return adjacency


@dataclass
class _Propagation:
    """单链的传播求解结果。"""

    earliest: Optional[datetime]
    predecessor: Optional[int]  # 直接来源链下标；污染源为 None
    via_edge: Optional[_ContactEdge]  # 直接来源 → 本链所沿的接触边


def _propagate(
    chain_count: int,
    sources: List[Tuple[int, datetime]],
    adjacency: Dict[int, List[_ContactEdge]],
    audit_until: datetime,
) -> List[_Propagation]:
    """以 Dijkstra 求各链最早污染时刻，并在同刻并列时固定唯一直接来源。

    松弛键依次为：

    1. 到达时刻 ``max(当前链已污染时刻, 接触起点)`` 最早；
    2. 时刻相同时，取传播路径的 chain_id 序列（下标按 chain_id 字典序编号，
       故下标序列即字典序）字典序最小者：先比较源头、再逐跳向后比较，
       等价于“同刻并列按 chain_id 字典序”且天然无环（前驱链恒为本链路径
       序列去掉末尾的严格前缀）。

    优先队列项为 ``(到达时刻, 路径序列, 链下标)``：同时刻下路径序列更小的
    链先出队，故任一链定案时得到的必为全局最小序列。污染源链以声明的
    contaminated_at 为种子、路径序列仅含自身，并冻结不接受传入污染。
    """
    dist: List[Optional[datetime]] = [None] * chain_count
    labels: List[Optional[Tuple[int, ...]]] = [None] * chain_count
    via: List[Optional[_ContactEdge]] = [None] * chain_count
    source_indices = frozenset(chain_index for chain_index, _ in sources)

    heap: List[Tuple[datetime, Tuple[int, ...], int]] = []
    for chain_index, contaminated_at in sources:
        dist[chain_index] = contaminated_at
        labels[chain_index] = (chain_index,)
        heapq.heappush(heap, (contaminated_at, (chain_index,), chain_index))

    while heap:
        current_at, current_label, u = heapq.heappop(heap)
        if current_label != labels[u] or current_at != dist[u]:
            continue  # 旧条目：该链此后得到过更早时刻或更小路径序列
        for edge in adjacency.get(u, []):
            v = edge.to_index
            if v in source_indices:
                continue  # 污染源以声明时刻为准，不接受传入污染
            # 源已污染且接触仍在持续的最早时刻
            candidate_at = max(current_at, edge.contact_start)
            if candidate_at >= edge.contact_end or candidate_at > audit_until:
                continue
            candidate_label = current_label + (v,)
            earlier = dist[v] is None or candidate_at < dist[v]
            same_time_better_label = (
                dist[v] is not None
                and candidate_at == dist[v]
                and (labels[v] is None or candidate_label < labels[v])
            )
            if not earlier and not same_time_better_label:
                continue
            dist[v] = candidate_at
            labels[v] = candidate_label
            via[v] = edge
            heapq.heappush(heap, (candidate_at, candidate_label, v))

    return [
        _Propagation(
            earliest=dist[i],
            predecessor=(labels[i][-2] if labels[i] is not None and len(labels[i]) > 1 else None),
            via_edge=(via[i] if labels[i] is not None and len(labels[i]) > 1 else None),
        )
        for i in range(chain_count)
    ]


def _build_path(
    chain_index: int,
    results: List[_Propagation],
    chain_ids: List[str],
) -> List[ContaminationHop]:
    """依直接来源从目标链回溯至源头，再反转为按传播先后排列的路径。"""
    reversed_hops: List[ContaminationHop] = []
    cursor = chain_index
    while results[cursor].predecessor is not None:
        predecessor = results[cursor].predecessor  # type: ignore[assignment]
        edge = results[cursor].via_edge  # type: ignore[assignment]
        source_at = results[predecessor].earliest  # type: ignore[assignment]
        reversed_hops.append(
            ContaminationHop(
                from_chain_id=chain_ids[predecessor],
                to_chain_id=chain_ids[cursor],
                custodian=edge.custodian,
                contact_start=edge.contact_start,
                contact_end=edge.contact_end,
                contaminated_at=max(source_at, edge.contact_start),
                from_event_index=edge.from_event_index,
                to_event_index=edge.to_event_index,
            )
        )
        cursor = predecessor
    reversed_hops.reverse()
    return reversed_hops


def trace_contamination(
    request: ContaminationTraceRequest,
) -> ContaminationTraceResponse:
    """校验各链并计算污染级联传播的最早时刻、直接来源与完整路径。"""
    # 请求链按 chain_id 字典序赋予稳定下标：Dijkstra 同刻并列即按 chain_id 定序
    ordered_chains = sorted(enumerate(request.chains), key=lambda item: item[1].chain_id)
    chain_ids = [chain.chain_id for _, chain in ordered_chains]
    chain_by_id: Dict[str, int] = {
        chain_id: index for index, chain_id in enumerate(chain_ids)
    }

    # 时区一致性：全部事件、污染源时刻与 audit_until 统一可比
    timestamps = [request.audit_until]
    timestamps.extend(source.contaminated_at for source in request.sources)
    for _, chain in ordered_chains:
        timestamps.extend(event.timestamp for event in chain.events)
    ensure_comparable(timestamps, ContaminationTraceError)

    # 复用单链校验；audit_until 不得早于任一链最后一个有效事件时间
    validations = [validate_events(chain.events) for _, chain in ordered_chains]
    for index, (_, chain) in enumerate(ordered_chains):
        last_valid: Optional[datetime] = None
        for event, result in zip(chain.events, validations[index].events):
            if result.status == "ok":
                last_valid = event.timestamp
        if last_valid is not None and request.audit_until < last_valid:
            raise ContaminationTraceError(
                "audit_until 早于链 "
                f"{chain.chain_id} 的最后一个有效事件时间："
                f"{request.audit_until.isoformat()} 早于 {last_valid.isoformat()}"
            )

    # 污染源必须全部已知（chain_id 重复由请求模型校验）
    sources: List[Tuple[int, datetime]] = []
    source_ids: set[str] = set()
    for source in request.sources:
        chain_index = chain_by_id.get(source.chain_id)
        if chain_index is None:
            raise ContaminationTraceError(f"未知污染源 chain_id：{source.chain_id}")
        sources.append((chain_index, source.contaminated_at))
        source_ids.add(source.chain_id)

    intervals_by_chain = [
        _derive_holding_intervals(chain.events, validations[index].events, request.audit_until)
        for index, (_, chain) in enumerate(ordered_chains)
    ]
    adjacency = _build_contact_edges(intervals_by_chain)
    results = _propagate(len(ordered_chains), sources, adjacency, request.audit_until)

    # 组装各链结果（chain_id 字典序），随后按请求输入顺序重排
    chain_results: List[ContaminationChainResult] = []
    contaminated_count = 0
    for index, chain_id in enumerate(chain_ids):
        propagation = results[index]
        reached = propagation.earliest is not None
        if reached:
            contaminated_count += 1
        # 污染源与未到达链路径均为空；其余链依直接来源回溯完整路径
        hops = (
            []
            if not reached or chain_id in source_ids
            else _build_path(index, results, chain_ids)
        )
        chain_results.append(
            ContaminationChainResult(
                chain_id=chain_id,
                reached=reached,
                is_source=chain_id in source_ids,
                earliest_contaminated_at=propagation.earliest,
                direct_source_chain_id=(
                    chain_ids[propagation.predecessor]
                    if reached and propagation.predecessor is not None
                    else None
                ),
                path=hops,
                validation=validations[index],
            )
        )

    # 按请求输入顺序返回各链结果
    result_by_id = {result.chain_id: result for result in chain_results}
    ordered_results = [result_by_id[chain.chain_id] for chain in request.chains]
    not_reached = sorted(
        result.chain_id for result in chain_results if not result.reached
    )

    return ContaminationTraceResponse(
        audit_until=request.audit_until,
        chains=ordered_results,
        not_reached_chain_ids=not_reached,
        summary=ContaminationTraceSummary(
            total=len(request.chains),
            sources=len(source_ids),
            contaminated=contaminated_count,
            not_reached=len(not_reached),
        ),
    )
