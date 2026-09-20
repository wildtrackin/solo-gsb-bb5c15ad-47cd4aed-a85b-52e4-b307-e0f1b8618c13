"""污染接触溯源接口的单元测试与 API 集成测试。"""
from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.contamination import trace_contamination
from app.main import app
from app.models import ContaminationSource, ContaminationTraceRequest, Event, EventType

client = TestClient(app)

DAY = "2026-09-20"


def dt(hour: int, minute: int = 0, second: int = 0, tz: str = "+00:00") -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hour:02d}:{minute:02d}:{second:02d}{tz}")


def ev(type_: EventType, hour: int, minute: int = 0, **kw) -> Event:
    return Event(type=type_, timestamp=dt(hour, minute), **kw)


def seal(seal_id: str, custodian: str, hour: int, minute: int = 0) -> Event:
    return ev(EventType.SEAL, hour, minute, seal_id=seal_id, custodian=custodian)


def handover(custodian: str, hour: int, minute: int = 0) -> Event:
    return ev(EventType.HANDOVER, hour, minute, custodian=custodian)


def unseal(hour: int, minute: int = 0) -> Event:
    return ev(EventType.UNSEAL, hour, minute)


def reseal(seal_id: str, custodian: str, hour: int, minute: int = 0) -> Event:
    return ev(EventType.RESEAL, hour, minute, seal_id=seal_id, custodian=custodian)


def chain(chain_id: str, events) -> dict:
    return {"chain_id": chain_id, "events": events}


def m(chain_id: str, hour: int, minute: int = 0) -> dict:
    return {"chain_id": chain_id, "contaminated_at": f"{DAY}T{hour:02d}:{minute:02d}:00Z"}


def make_request(chains, sources, audit=(20, 0)) -> ContaminationTraceRequest:
    return ContaminationTraceRequest(
        chains=[chain(cid, list(events)) for cid, events in chains],
        sources=[ContaminationSource(chain_id=cid, contaminated_at=at) for cid, at in sources],
        audit_until=dt(*audit),
    )


def post(chains, sources, audit=(20, 0)):
    payload = {
        "chains": [
            chain(cid, [event.model_dump(mode="json") for event in events])
            for cid, events in chains
        ],
        "sources": [
            {"chain_id": cid, "contaminated_at": at.isoformat()}
            for cid, at in sources
        ],
        "audit_until": dt(*audit).isoformat(),
    }
    return client.post("/api/v1/seal-chain/contamination-trace", json=payload)


def result_by_id(resp, chain_id: str):
    return next(item for item in resp["chains"] if item["chain_id"] == chain_id)


# ---------------------------------------------------------------------------
# 基本级联：同保管人接触 → 级联传播 → 完整路径
# ---------------------------------------------------------------------------


def test_basic_cascade_with_path():
    # A 自 10:00 污染，全程在 Alice 处；B 09:00-11:00 在 Alice，11:00 交给 Bob；
    # C 自 11:30 在 Bob。污染 A→B（10:00）→C（11:30）。
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9), handover("Bob", 11)]),
                ("C", [seal("SC", "Bob", 11, 30)]),
            ],
            sources=[("A", dt(10))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}

    assert by_id["A"].reached is True
    assert by_id["A"].is_source is True
    assert by_id["A"].earliest_contaminated_at == dt(10)
    assert by_id["A"].direct_source_chain_id is None
    assert by_id["A"].path == []

    assert by_id["B"].earliest_contaminated_at == dt(10)
    assert by_id["B"].direct_source_chain_id == "A"
    assert len(by_id["B"].path) == 1
    hop = by_id["B"].path[0]
    assert hop.from_chain_id == "A"
    assert hop.to_chain_id == "B"
    assert hop.custodian == "Alice"
    assert hop.contact_start == dt(9)
    assert hop.contact_end == dt(11)
    assert hop.contaminated_at == dt(10)
    assert hop.from_event_index == 0
    assert hop.to_event_index == 0

    assert by_id["C"].earliest_contaminated_at == dt(11, 30)
    assert by_id["C"].direct_source_chain_id == "B"
    assert [h.to_chain_id for h in by_id["C"].path] == ["B", "C"]
    assert by_id["C"].path[1].custodian == "Bob"
    assert by_id["C"].path[1].contact_start == dt(11, 30)
    assert by_id["C"].path[1].contaminated_at == dt(11, 30)

    assert resp.not_reached_chain_ids == []
    assert resp.summary.model_dump() == {
        "total": 3,
        "sources": 1,
        "contaminated": 3,
        "not_reached": 0,
    }


def test_no_contact_when_custodians_differ():
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Bob", 8)]),
            ],
            sources=[("A", dt(9))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["A"].reached is True
    assert by_id["B"].reached is False
    assert by_id["B"].earliest_contaminated_at is None
    assert by_id["B"].direct_source_chain_id is None
    assert by_id["B"].path == []
    assert resp.not_reached_chain_ids == ["B"]


def test_no_contact_when_intervals_only_touch_half_open():
    # 半开区间：A 的持有 08:00-10:00，B 自 10:00 起 —— 仅相接，不构成接触
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8), handover("Bob", 10)]),
                ("B", [seal("SB", "Alice", 10)]),
            ],
            sources=[("A", dt(9))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].reached is False
    assert resp.not_reached_chain_ids == ["B"]


def test_contact_follows_per_interval_custodian_not_chain_identity():
    # A：08-10 在 Alice，10:00 交给 Bob（之后一直在 Bob）；09:00 已污染
    # C：全程在 Bob。污染随展品 A 在 10:00 交接后继续存在，
    # A 与 C 的保管人区间 [10:00,...) 重叠 → 接触自 10:00 起，C 于 10:00 污染
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8), handover("Bob", 10)]),
                ("C", [seal("SC", "Bob", 8)]),
            ],
            sources=[("A", dt(9))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["C"].reached is True
    assert by_id["C"].direct_source_chain_id == "A"
    assert by_id["C"].earliest_contaminated_at == dt(10)
    hop = by_id["C"].path[0]
    assert hop.custodian == "Bob"
    assert hop.contact_start == dt(10)
    assert hop.from_event_index == 1  # A 的 Bob 区间由 handover 事件开启


def test_contact_only_within_overlapping_custodian_window():
    # A 08-12 在 Alice；B 08-10 在 Alice，10:00 交给 Carol
    # C 全程在 Bob。A 09:00 污染：B 可在 09:00 经 Alice 污染；C 无接触
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 8), handover("Carol", 10)]),
                ("C", [seal("SC", "Bob", 8)]),
            ],
            sources=[("A", dt(9))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].reached is True
    assert by_id["B"].earliest_contaminated_at == dt(9)
    assert by_id["C"].reached is False
    assert resp.not_reached_chain_ids == ["C"]


# ---------------------------------------------------------------------------
# 传播时刻：max(源已污染时刻, 接触起点)、接触结束边界、audit_until 边界
# ---------------------------------------------------------------------------


def test_contamination_after_contact_ended_does_not_propagate():
    # A、B 在 Alice 处重叠 [09:00,11:00)，A 12:00 才污染：接触早已结束
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8), handover("Zoe", 11)]),
                ("B", [seal("SB", "Alice", 9), handover("Bob", 11)]),
            ],
            sources=[("A", dt(12))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["A"].reached is True
    assert by_id["B"].reached is False
    assert resp.not_reached_chain_ids == ["B"]


def test_contamination_mid_contact_propagates_immediately():
    # 接触 [09:00,11:00)，A 10:30 才污染 → B 10:30 被污染
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9), handover("Bob", 11)]),
            ],
            sources=[("A", (dt(10, 30)))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].earliest_contaminated_at == dt(10, 30)


def test_source_contaminated_before_contact_waits_for_contact_start():
    # A 08:30 已污染，但 A 09:00 才到 Alice 处与 B 接触 → B 09:00 污染
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Zoe", 8), handover("Alice", 9)]),
                ("B", [seal("SB", "Alice", 9)]),
            ],
            sources=[("A", dt(8, 30))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].earliest_contaminated_at == dt(9)
    assert by_id["B"].path[0].contaminated_at == dt(9)


def test_transfer_at_contact_end_excluded_half_open():
    # 接触 [09:00,11:00)，传播时刻恰好为 11:00 时接触已结束 → 不传播
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9), handover("Bob", 11)]),
            ],
            sources=[("A", dt(11))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].reached is False


def test_propagation_exactly_at_audit_until_excluded():
    # 接触 [10:00, 向右开放)，A 12:00 污染，audit_until=12:00：恰在截止点不计
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9)]),
            ],
            sources=[("A", dt(12))],
            audit=(12, 0),
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].reached is False
    # 污染源自身仍计为已污染
    assert by_id["A"].reached is True


def test_propagation_just_before_audit_until_included():
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9)]),
            ],
            sources=[("A", dt(11, 59, 59))],
            audit=(12, 0),
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].reached is True
    assert by_id["B"].earliest_contaminated_at == dt(11, 59, 59)


def test_open_ended_interval_clamped_at_audit_until():
    # 两链最后一个持有区间向右开放，接触段终点应为 audit_until
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9)]),
            ],
            sources=[("A", dt(10))],
            audit=(15, 0),
        )
    )
    hop = resp.chains[1].path[0]
    assert hop.contact_start == dt(9)
    assert hop.contact_end == dt(15)


# ---------------------------------------------------------------------------
# unseal 不改变保管人；违规事件不产生区间
# ---------------------------------------------------------------------------


def test_unseal_does_not_change_custodian_interval():
    # B 在 Alice 名下：seal 09:00，unseal 10:00，reseal(Bob? no, Alice) 10:30
    events_b = [
        seal("SB1", "Alice", 9),
        unseal(10),
        reseal("SB2", "Alice", 10, 30),
    ]
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", events_b),
            ],
            sources=[("A", dt(11))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    # unseal 不闭合保管人区间；B 全程在 Alice 名下（仅 reseal 开启新区间），
    # 与 A 的重叠覆盖 11:00
    assert by_id["B"].reached is True
    assert by_id["B"].validation.valid is True


def test_violation_events_generate_no_interval():
    # B 首个事件为未封装 handover（违规，不产生区间），09:00 才合法 seal
    events_b = [
        handover("Alice", 8),
        seal("SB", "Alice", 9),
    ]
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", events_b),
            ],
            sources=[("A", dt(8, 30))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    # B 的持有自 09:00 开始；接触 [09:00, ...)，A 08:30 已污染 → B 09:00 污染
    assert by_id["B"].reached is True
    assert by_id["B"].earliest_contaminated_at == dt(9)
    assert by_id["B"].validation.valid is False
    assert by_id["B"].validation.violations[0].index == 0


# ---------------------------------------------------------------------------
# 同刻并列：按 chain_id 字典序固定直接来源
# ---------------------------------------------------------------------------


def test_earliest_time_tie_breaks_by_chain_id_lexicographic():
    # A、B 均自 10:00 污染（两个源），C 自 10:00 起同时与 A、B 在同一保管人处
    # 接触 → C 的直接来源取字典序更小的 A
    resp = trace_contamination(
        make_request(
            chains=[
                ("B", [seal("SB", "Alice", 9)]),
                ("C", [seal("SC", "Alice", 10)]),
                ("A", [seal("SA", "Alice", 9)]),
            ],
            sources=[("B", dt(10)), ("A", dt(10))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["C"].earliest_contaminated_at == dt(10)
    assert by_id["C"].direct_source_chain_id == "A"
    assert by_id["C"].path[0].from_chain_id == "A"


def test_earliest_time_tie_via_propagated_sources():
    # A 与 X 都在 10:00 被污染后，于 10:30 分别与 C 接触（不同保管人，
    # 同一时刻完成传播）→ C 的直接来源取字典序更小的 A
    # A 10:00 污染（源），经 Alice 传给 B；X 10:00 污染（源），经 Xavier 传给 B
    # B 从 10:30 起同时处于 Alice 与 Xavier 名下不可能（单展品单保管人），
    # 故构造：B 10:30 在 Alice 处接触 A、同时在 Alice 处接触 X（三者同保管人）
    resp = trace_contamination(
        make_request(
            chains=[
                ("X", [seal("SX", "Alice", 10, 30)]),
                ("A", [seal("SA", "Alice", 10, 30)]),
                ("B", [seal("SB", "Alice", 10, 30)]),
            ],
            sources=[("X", dt(10, 30)), ("A", dt(10, 30))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].earliest_contaminated_at == dt(10, 30)
    assert by_id["B"].direct_source_chain_id == "A"


def test_independent_earlier_path_beats_later_contact():
    # C 可经 A 在 12:00 到达、经 B 在 11:00 到达：取最早时刻 11:00（B）
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Bob", 8)]),
                ("C", [seal("SC1", "Bob", 11), handover("Alice", 12)]),
            ],
            sources=[("A", dt(8)), ("B", dt(8))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["C"].earliest_contaminated_at == dt(11)
    assert by_id["C"].direct_source_chain_id == "B"
    assert [h.from_chain_id for h in by_id["C"].path] == ["B"]


# ---------------------------------------------------------------------------
# 多源与未到达链
# ---------------------------------------------------------------------------


def test_multiple_sources_and_not_reached_listing():
    resp = trace_contamination(
        make_request(
            chains=[
                ("zeta", [seal("SZ", "Alice", 8)]),
                ("alpha", [seal("SA", "Bob", 8)]),
                ("mid", [seal("SM", "Carol", 8)]),
            ],
            sources=[("zeta", dt(9)), ("alpha", dt(9))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["zeta"].is_source is True
    assert by_id["alpha"].is_source is True
    assert by_id["mid"].reached is False
    # 未到达链按 chain_id 字典序
    assert resp.not_reached_chain_ids == ["mid"]
    assert resp.summary.not_reached == 1
    assert resp.summary.contaminated == 2
    assert resp.summary.sources == 2


def test_results_returned_in_request_order_even_with_sources():
    resp = trace_contamination(
        make_request(
            chains=[
                ("c3", [seal("S3", "Alice", 8)]),
                ("c1", [seal("S1", "Alice", 8)]),
                ("c2", [seal("S2", "Alice", 8)]),
            ],
            sources=[("c1", dt(9))],
        )
    )
    assert [item.chain_id for item in resp.chains] == ["c3", "c1", "c2"]
    # 全部同保管人接触，c2 与 c3 都经 c1 在 09:00 污染（接触起点 08:00）
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["c2"].earliest_contaminated_at == dt(9)
    assert by_id["c3"].earliest_contaminated_at == dt(9)


def test_result_independent_of_input_order():
    chains = [
        ("A", [seal("SA", "Alice", 8)]),
        ("B", [seal("SB", "Alice", 9), handover("Bob", 11)]),
        ("C", [seal("SC", "Bob", 11, 30)]),
        ("D", [seal("SD", "Zoe", 8)]),
    ]
    sources = [("A", dt(10))]
    first = trace_contamination(make_request(chains=chains, sources=sources))
    shuffled = trace_contamination(
        make_request(chains=list(reversed(chains)), sources=list(reversed(sources)))
    )

    def canonical(resp):
        return sorted(
            (
                item.chain_id,
                item.reached,
                item.earliest_contaminated_at.isoformat()
                if item.earliest_contaminated_at
                else None,
                item.direct_source_chain_id,
                [
                    (
                        hop.from_chain_id,
                        hop.to_chain_id,
                        hop.custodian,
                        hop.contaminated_at.isoformat(),
                    )
                    for hop in item.path
                ],
            )
            for item in resp.chains
        )

    assert canonical(first) == canonical(shuffled)
    assert first.not_reached_chain_ids == shuffled.not_reached_chain_ids == ["D"]


def test_branch_dead_end_does_not_block_other_branch():
    # A 在 Alice 处同时接触 B 与 D；B 经 Bob 传到 C，D 之后离开 Alice 且无后续接触
    # → B、C 到达；D 同样在与 A 重叠期间被污染（不存在“死胡同阻断”，污染独立传播）
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 9), handover("Bob", 12)]),
                ("C", [seal("SC", "Bob", 12, 30)]),
                ("D", [seal("SD", "Alice", 9), handover("Dan", 10)]),
            ],
            sources=[("A", dt(9, 30))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].earliest_contaminated_at == dt(9, 30)
    assert by_id["D"].earliest_contaminated_at == dt(9, 30)
    assert by_id["C"].earliest_contaminated_at == dt(12, 30)
    assert [hop.to_chain_id for hop in by_id["C"].path] == ["B", "C"]
    assert resp.not_reached_chain_ids == []


def test_contact_that_begins_after_source_pollution_uses_contact_start():
    # 源 08:00 污染，但与 B 的共同保管人接触 10:00 才开始（A 10:00 才交给 Alice）
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Zoe", 8), handover("Alice", 10)]),
                ("B", [seal("SB", "Alice", 9)]),
            ],
            sources=[("A", dt(8))],
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["B"].earliest_contaminated_at == dt(10)
    assert by_id["B"].path[0].from_event_index == 1
    assert by_id["B"].path[0].to_event_index == 0


def test_dense_equal_time_clusters_produce_acyclic_canonical_paths():
    # 多链、多保管人、交接与重封的稠密场景：所有到达路径必须终止于某个污染源，
    # 逐跳链标识首尾相接且无重复（回归同刻簇下直接来源成环的问题）。
    chains = [
        ("a01", [seal("a01-1", "Alice", 8), handover("Bob", 9), handover("Carol", 10)]),
        ("a02", [seal("a02-1", "Alice", 8), handover("Bob", 9, 30), unseal(10),
                 reseal("a02-2", "Bob", 10, 30)]),
        ("a03", [seal("a03-1", "Bob", 9), handover("Carol", 11)]),
        ("a04", [seal("a04-1", "Carol", 8), handover("Bob", 10, 30)]),
        ("a05", [seal("a05-1", "Alice", 9, 30), handover("Carol", 10, 30)]),
        ("a06", [seal("a06-1", "Zoe", 8)]),
    ]
    resp = trace_contamination(
        make_request(chains=chains, sources=[("a04", dt(9)), ("a01", dt(9, 30))])
    )
    by_id = {item.chain_id: item for item in resp.chains}
    for item in resp.chains:
        if not item.reached or item.is_source:
            assert item.path == []
            continue
        # 路径首跳来源必须是污染源，末跳目标必须是本链，逐跳首尾相接
        assert by_id[item.path[0].from_chain_id].is_source is True
        assert item.path[-1].to_chain_id == item.chain_id
        visited = []
        for hop in item.path:
            # 相邻两跳首尾相接：本跳来源 = 上一跳目标
            if visited:
                assert hop.from_chain_id == visited[-1]
            visited.append(hop.from_chain_id)
            assert hop.to_chain_id not in visited  # 无环
            visited.append(hop.to_chain_id)
            assert hop.from_chain_id != hop.to_chain_id
        # 直接来源与路径末跳一致
        assert item.direct_source_chain_id == item.path[-1].from_chain_id
    # a06 全程在 Zoe 处，与任何污染源都无接触
    assert by_id["a06"].reached is False
    assert resp.not_reached_chain_ids == ["a06"]


def test_source_contaminated_after_audit_until_reaches_nobody():
    # 源自身仍标记已污染（声明时刻），但晚于 audit_until 的污染不能传播
    resp = trace_contamination(
        make_request(
            chains=[
                ("A", [seal("SA", "Alice", 8)]),
                ("B", [seal("SB", "Alice", 8)]),
            ],
            sources=[("A", dt(15))],
            audit=(12, 0),
        )
    )
    by_id = {item.chain_id: item for item in resp.chains}
    assert by_id["A"].reached is True
    assert by_id["A"].earliest_contaminated_at == dt(15)
    assert by_id["B"].reached is False
    assert resp.not_reached_chain_ids == ["B"]


# ---------------------------------------------------------------------------
# 422：混用时区 / 未知污染源 / 重复 chain_id / audit_until 过早
# ---------------------------------------------------------------------------


def test_unknown_source_chain_id_returns_422():
    r = post(
        chains=[("A", [seal("SA", "Alice", 8)])],
        sources=[("GHOST", dt(9))],
    )
    assert r.status_code == 422
    assert "GHOST" in r.json()["detail"]


def test_duplicate_source_chain_id_returns_422():
    payload = {
        "chains": [{"chain_id": "A", "events": []}],
        "sources": [m("A", 9), m("A", 10)],
        "audit_until": f"{DAY}T20:00:00Z",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 422


def test_duplicate_chain_id_returns_422():
    payload = {
        "chains": [
            {"chain_id": "A", "events": []},
            {"chain_id": "A", "events": []},
        ],
        "sources": [],
        "audit_until": f"{DAY}T20:00:00Z",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 422


def test_mixed_timezone_events_returns_422():
    payload = {
        "chains": [
            {
                "chain_id": "A",
                "events": [
                    {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
                     "seal_id": "SA", "custodian": "Alice"},
                    {"type": "handover", "timestamp": f"{DAY}T09:00:00",
                     "custodian": "Bob"},
                ],
            }
        ],
        "sources": [],
        "audit_until": f"{DAY}T20:00:00Z",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 422
    assert "时区" in r.json()["detail"]


def test_mixed_timezone_source_returns_422():
    payload = {
        "chains": [
            {"chain_id": "A", "events": [
                {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
                 "seal_id": "SA", "custodian": "Alice"}
            ]}
        ],
        "sources": [{"chain_id": "A", "contaminated_at": f"{DAY}T09:00:00"}],
        "audit_until": f"{DAY}T20:00:00Z",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 422
    assert "时区" in r.json()["detail"]


def test_mixed_timezone_audit_until_returns_422():
    payload = {
        "chains": [
            {"chain_id": "A", "events": [
                {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
                 "seal_id": "SA", "custodian": "Alice"}
            ]}
        ],
        "sources": [],
        "audit_until": f"{DAY}T20:00:00",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 422
    assert "时区" in r.json()["detail"]


def test_audit_until_before_last_valid_event_returns_422():
    payload = {
        "chains": [
            {"chain_id": "A", "events": [
                {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
                 "seal_id": "SA", "custodian": "Alice"},
                {"type": "handover", "timestamp": f"{DAY}T18:00:00Z",
                 "custodian": "Bob"},
            ]}
        ],
        "sources": [],
        "audit_until": f"{DAY}T12:00:00Z",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 422
    assert "audit_until" in r.json()["detail"]


def test_audit_until_equal_last_valid_event_is_allowed():
    payload = {
        "chains": [
            {"chain_id": "A", "events": [
                {"type": "seal", "timestamp": f"{DAY}T08:00:00Z",
                 "seal_id": "SA", "custodian": "Alice"}
            ]}
        ],
        "sources": [],
        "audit_until": f"{DAY}T08:00:00Z",
    }
    r = client.post("/api/v1/seal-chain/contamination-trace", json=payload)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# API 集成与空请求
# ---------------------------------------------------------------------------


def test_api_empty_request_returns_200():
    r = client.post(
        "/api/v1/seal-chain/contamination-trace",
        json={"audit_until": f"{DAY}T20:00:00Z"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["chains"] == []
    assert body["not_reached_chain_ids"] == []
    assert body["summary"] == {
        "total": 0,
        "sources": 0,
        "contaminated": 0,
        "not_reached": 0,
    }


def test_api_full_cascade_shape():
    r = post(
        chains=[
            ("A", [seal("SA", "Alice", 8)]),
            ("B", [seal("SB", "Alice", 9), handover("Bob", 11)]),
            ("C", [seal("SC", "Bob", 11, 30)]),
        ],
        sources=[("A", dt(10))],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["audit_until"] == f"{DAY}T20:00:00Z"
    c_result = result_by_id(body, "C")
    assert c_result["earliest_contaminated_at"] == f"{DAY}T11:30:00Z"
    assert c_result["direct_source_chain_id"] == "B"
    assert [hop["to_chain_id"] for hop in c_result["path"]] == ["B", "C"]
    # 单链校验原样保留
    assert c_result["validation"]["final_custodian"] == "Bob"


def test_existing_endpoints_unchanged():
    r = client.post(
        "/api/v1/seal-chain/validate",
        json={
            "events": [
                {"type": "seal", "timestamp": f"{DAY}T10:00:00Z",
                 "seal_id": "S1", "custodian": "Alice"}
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["valid"] is True
