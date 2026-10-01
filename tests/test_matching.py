"""The three matching rules against real records from the pinned ROR release.

tests/fixtures/matching_golden.json is the golden file: a sparse subgraph of
real v2.13 organisations (relationship lists copied unmodified from the dump)
plus hand-checked cases. Each case names the real organisations it uses. The
organisations, by ROR id:

  052gg0110  University of Oxford ............. 19 children, 7 related
  03h2bh287  Oxford University Hospitals NHS Trust ... 8 children, incl. the
                                               John Radcliffe
  0080acb59  John Radcliffe Hospital .......... child of the trust, related
                                               to Oxford (asserted both ends)
  01ahsqc77  Kennedy Institute of Rheumatology .. child of Oxford
  034t30j35  Chinese Academy of Sciences ...... 121 children, incl. the
                                               Institute of Physics
  05cvf7v30  Institute of Physics (CAS) ....... parent CAS, 3 children
  04dshd728  State Key Laboratory of Magnetism .. child of the Institute of
                                               Physics: two hops from CAS
  00w0f8567  Kyushu Tokai University .......... inactive, successor Tokai
  01p7qe739  Tokai University ................. related edges only
  03fpf5m04  Weston Area Health NHS Trust ..... inactive, successor UHBW:
                                               first link of a two-step chain
  03jzzxg14  University Hospitals Bristol and Weston NHS FT ... inactive,
                                               predecessor Weston, successor
                                               Bristol NHS FT
  054vvq170  Bristol NHS Foundation Trust ..... names UHBW as predecessor:
                                               end of the chain
  03bdvdc06  Public Library of Science ........ withdrawn, successor below
  008zgvp64  Public Library of Science ........ active, same display name
  02e16g702  Hokkaido University .............. related to its hospital
  0419drx70  Hokkaido University Hospital ..... related to the university
  0038gz437  Newman University (Wichita, US) .. no relationships
  009tnsj43  Newman University (Birmingham, UK) . the geographic namesake
  04xftk194  De La Salle University ........... active, one related edge
  012mgrb02  De La Salle Medical and Health Sciences Institute ... active,
                                               names the University as a
                                               predecessor: a live successor
                                               of a live record
  04dtfyh05  Peninsula College of Medicine and Dentistry ... inactive,
                                               related and successor edges
                                               to Plymouth and to Exeter
  008n7pv89  University of Plymouth ........... related edges only
  05fkxeh29  Sudbury Regional Hospital ........ inactive, parent and
                                               successor edges to Health
                                               Sciences North
  04br0rs05  Health Sciences North ............ one child, asserts nothing
                                               back to Sudbury
  03mc19e15  Weston General Hospital .......... active, parent Bristol NHS
                                               FT; Weston lists it as a child

A case carries either `expected` (per-rule outcomes and counts) or
`expected_error` (the exception the scorer must raise and the strings its
message must mention); the loader below splits the cases on that.

The second half of the file checks the things a golden file cannot: that the
dump loader satisfies the graph protocol and gives the same answers, that
malformed input is refused rather than scored, and that the scoring function
has no way of knowing which source it is scoring.
"""

from __future__ import annotations

import inspect
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from disambig import matching
from disambig.matching import (
    AncestryIndex,
    ElementCounts,
    GoldLabel,
    GraphRecord,
    MatchRule,
    Outcome,
    PairRelation,
    Relation,
    RuleScore,
    StaticRorGraph,
    SuccessionIndex,
    relation_between,
    score_item,
    succession_index_for,
    sum_element_counts,
)
from disambig.models import Decision, Label
from disambig.ror_dump import RelationshipSets, RorDump, RorDumpPin
from disambig.ror_ids import normalise_ror_id

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "matching_golden.json"
DUMP_SAMPLE = FIXTURES / "ror_dump_sample.json"
PIN = Path(__file__).parent.parent / "config" / "ror_dump.yaml"

OXFORD = "https://ror.org/052gg0110"
OUH_TRUST = "https://ror.org/03h2bh287"
JOHN_RADCLIFFE = "https://ror.org/0080acb59"
KENNEDY = "https://ror.org/01ahsqc77"
CAS = "https://ror.org/034t30j35"
IOP = "https://ror.org/05cvf7v30"
SKLM = "https://ror.org/04dshd728"
CRUK_OXFORD_CENTRE = "https://ror.org/05kgg0s20"  # child of both Oxford and the trust
CRUK_MRC_INSTITUTE = "https://ror.org/011hz4254"  # child of Oxford, no record in the fixture
HOKKAIDO = "https://ror.org/02e16g702"
KYUSHU_TOKAI = "https://ror.org/00w0f8567"  # inactive, successor Tokai
TOKAI = "https://ror.org/01p7qe739"
WESTON = "https://ror.org/03fpf5m04"  # inactive, successor UHBW
UHBW = "https://ror.org/03jzzxg14"  # inactive, successor Bristol NHS FT
BRISTOL_NHS_FT = "https://ror.org/054vvq170"
WESTON_GENERAL = "https://ror.org/03mc19e15"  # active, child of Weston, parent Bristol NHS FT
DLSU = "https://ror.org/04xftk194"  # active, named as predecessor by the institute below
DLS_INSTITUTE = "https://ror.org/012mgrb02"  # active
PENINSULA = "https://ror.org/04dtfyh05"  # inactive, related and successor edges to Plymouth
PLYMOUTH = "https://ror.org/008n7pv89"
SUDBURY = "https://ror.org/05fkxeh29"  # inactive, parent and successor edges to HSN
HEALTH_SCIENCES_NORTH = "https://ror.org/04br0rs05"

REQUIRED_CASES = frozenset(
    {
        "exact_hit",
        "parent_assigned_child_gold",
        "child_assigned_parent_gold",
        "related_hit",
        "stale_renamed",
        "stale_two_step_chain",
        "stale_duplicate_beside_correct_id",
        "stale_sibling_in_multi_affiliation",
        "stale_with_related_edge",
        "stale_with_hierarchy_edge",
        "stale_hierarchy_duplicate_beside_correct_id",
        "dead_id_with_hierarchy_edge_but_no_chain",
        "live_predecessor_assigned_live_successor_gold",
        "live_successor_assigned_live_predecessor_gold",
        "successor_assigned_inactive_gold",
        "geographic_namesake",
        "multi_affiliation_full_hit",
        "multi_affiliation_collapse",
        "gold_ambiguous_with_assignment",
        "gold_no_ror_with_assignment",
        "gold_no_ror_with_two_assigned_ids",
        "gold_no_ror_without_assignment",
        "empty_assignment",
    }
)
# Succession relations ground nothing under any rule (gate decision 1).
SUCCESSION_RELATIONS = frozenset({Relation.PREDECESSOR_OF_GOLD, Relation.SUCCESSOR_OF_GOLD})


def load_golden() -> dict[str, Any]:
    loaded = json.loads(GOLDEN.read_text())
    assert isinstance(loaded, dict)
    return loaded


def build_graph(fixture: dict[str, Any]) -> StaticRorGraph:
    records: dict[str, GraphRecord] = {}
    for ror_id, entry in fixture["graph"].items():
        relationships = entry["relationships"]
        records[ror_id] = GraphRecord(
            status=entry["status"],
            relationships=RelationshipSets(
                **{rel_type: frozenset(ids) for rel_type, ids in relationships.items()}
            ),
        )
    return StaticRorGraph(records)


GOLDEN_FIXTURE = load_golden()
GOLDEN_GRAPH = build_graph(GOLDEN_FIXTURE)
GOLDEN_CASES: list[dict[str, Any]] = GOLDEN_FIXTURE["cases"]
CASE_IDS = [case["id"] for case in GOLDEN_CASES]
# Cases the scorer scores, and cases it must refuse. Every case is one or the other.
SCORED_CASES = [case for case in GOLDEN_CASES if "expected_error" not in case]
ERROR_CASES = [case for case in GOLDEN_CASES if "expected_error" in case]


def expected_error(case: dict[str, Any]) -> type[Exception]:
    spec = case["expected_error"]
    assert spec["type"] == "ValueError", spec  # the only error the scorer raises by design
    assert spec["mentions"], spec
    return ValueError


def gold_from_case(case: dict[str, Any]) -> GoldLabel:
    return GoldLabel(
        decision=Decision(case["gold"]["decision"]),
        ror_ids=frozenset(case["gold"]["ror_ids"]),
    )


def expected_rule_score(rule: MatchRule, expected: dict[str, Any]) -> RuleScore:
    return RuleScore(
        rule=rule,
        outcome=Outcome(expected["outcome"]),
        true_positives=expected["true_positives"],
        false_positives=expected["false_positives"],
        false_negatives=expected["false_negatives"],
    )


# --- golden file -----------------------------------------------------------


def test_golden_file_is_pinned_to_the_configured_ror_release() -> None:
    pin = RorDumpPin.from_yaml(PIN)
    assert GOLDEN_FIXTURE["ror_dump_version"] == pin.version
    assert GOLDEN_FIXTURE["ror_dump_publication_date"] == pin.publication_date


def test_golden_file_covers_the_required_cases() -> None:
    assert set(CASE_IDS) >= REQUIRED_CASES
    assert len(CASE_IDS) >= 15
    assert len(set(CASE_IDS)) == len(CASE_IDS)
    assert len(SCORED_CASES) + len(ERROR_CASES) == len(GOLDEN_CASES)
    for case in GOLDEN_CASES:
        assert ("expected" in case) != ("expected_error" in case), case["id"]
    assert ERROR_CASES, "the non-active gold id error must be pinned by a golden case"


def is_active(ror_id: str, graph: StaticRorGraph | RorDump) -> bool:
    return ror_id in graph and graph.status(ror_id) == "active"


def test_golden_graph_holds_real_looking_records() -> None:
    assert len(GOLDEN_GRAPH) == 25
    for ror_id, entry in GOLDEN_FIXTURE["graph"].items():
        assert normalise_ror_id(ror_id) == ror_id
        assert entry["status"] in {"active", "inactive", "withdrawn"}
        assert entry["name"]
    # The namesakes really do share a display name across two countries.
    us = GOLDEN_FIXTURE["graph"]["https://ror.org/0038gz437"]
    uk = GOLDEN_FIXTURE["graph"]["https://ror.org/009tnsj43"]
    assert us["name"] == uk["name"] == "Newman University"
    assert us["country"] != uk["country"]


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=CASE_IDS)
def test_golden_rule_outcomes(case: dict[str, Any]) -> None:
    if "expected_error" in case:
        with pytest.raises(expected_error(case)) as raised:
            score_item(gold_from_case(case), case["assigned"], GOLDEN_GRAPH)
        for mention in case["expected_error"]["mentions"]:
            assert mention in str(raised.value), mention
        return
    result = score_item(gold_from_case(case), case["assigned"], GOLDEN_GRAPH)
    for rule in MatchRule:
        assert result.by_rule[rule] == expected_rule_score(rule, case["expected"][rule.value]), (
            rule
        )
    assert result.has_assignment == bool(case["assigned"])


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=CASE_IDS)
def test_golden_pair_relations(case: dict[str, Any]) -> None:
    expected = tuple(
        PairRelation(pair["assigned"], pair["gold"], Relation(pair["relation"]))
        for pair in case["expected_relations"]
    )
    if "expected_error" in case:
        # The scorer refuses the item, but the relation stays computable for
        # diagnostics on a raw label file, which is why SUCCESSOR_OF_GOLD is
        # still in the enum.
        gold = gold_from_case(case)
        relations = tuple(
            PairRelation(a, g, relation_between(a, g, GOLDEN_GRAPH))
            for a in sorted(normalise_ror_id(value) for value in case["assigned"])
            for g in sorted(gold.ror_ids)
        )
        assert relations == expected
        return
    result = score_item(gold_from_case(case), case["assigned"], GOLDEN_GRAPH)
    assert result.relations == expected


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=CASE_IDS)
def test_golden_ancestry_distance(case: dict[str, Any]) -> None:
    index = AncestryIndex(GOLDEN_GRAPH)
    gold = gold_from_case(case)
    assert index.min_distance(case["assigned"], gold.ror_ids) == case["expected_ancestry_distance"]
    if "expected_ancestry_distance_at_max_depth_1" in case:
        assert (
            index.min_distance(case["assigned"], gold.ror_ids, max_depth=1)
            == case["expected_ancestry_distance_at_max_depth_1"]
        )


def test_golden_counts_follow_the_outcome_contract() -> None:
    # Counts are all present or all absent. Ambiguous gold and abstentions
    # never carry them; a no_ror gold carries them exactly when something was
    # assigned, and then only as false positives (gate decision 2); every other
    # outcome always carries them. Abstentions are scorable (they belong in
    # the coverage denominator) but not counted, and a no_ror item with an
    # assignment is counted but not scorable, which is why the two properties
    # are kept apart.
    for case in SCORED_CASES:
        gold = gold_from_case(case)
        result = score_item(gold, case["assigned"], GOLDEN_GRAPH)
        for score in result.by_rule.values():
            counts = (score.true_positives, score.false_positives, score.false_negatives)
            if score.counted:
                assert None not in counts, case["id"]
            else:
                assert counts == (None, None, None), case["id"]
                assert not score.hit, case["id"]
            if score.outcome is Outcome.NO_ASSIGNMENT:
                assert score.scorable and not score.counted, case["id"]
            elif score.outcome is Outcome.GOLD_AMBIGUOUS:
                assert not score.scorable and not score.counted, case["id"]
            elif score.outcome is Outcome.GOLD_NO_ROR:
                assert not score.scorable, case["id"]
                assert score.counted == result.has_assignment, case["id"]
                if score.counted:
                    assert counts == (0, len(result.assigned), 0), case["id"]
            else:
                assert score.scorable and score.counted, case["id"]
        assert result.scorable == gold.resolved


def test_stale_needs_an_empty_matching_and_is_rule_independent() -> None:
    # Decision from the audit: STALE means the whole assignment is dead ids of
    # the right institutions. As soon as one assigned id grounds, the item is
    # PARTIAL, with the dead id still carried in stale_assigned. Gate decision
    # 1, tightened: a non-active id grounds nothing under any rule, so an item
    # is STALE under one rule exactly when it is STALE under all three with
    # the same counts, and that happens exactly when stale_assigned is the
    # whole non-empty assignment. stale_assigned is re-derived here from the
    # graph (status plus the successor chain), independently of the scorer.
    succession = succession_index_for(GOLDEN_GRAPH)
    stale_items = 0
    for case in SCORED_CASES:
        result = score_item(gold_from_case(case), case["assigned"], GOLDEN_GRAPH)
        expected_stale = {
            a
            for a in result.assigned
            if not is_active(a, GOLDEN_GRAPH)
            and any(succession.supersedes(a, g) for g in result.gold.ror_ids)
        }
        assert result.stale_assigned == expected_stale, case["id"]
        scores = list(result.by_rule.values())
        is_stale = bool(result.assigned) and result.stale_assigned == result.assigned
        for score in scores:
            assert (score.outcome is Outcome.STALE) == is_stale, (case["id"], score.rule)
            if result.stale_assigned and score.true_positives:
                assert score.outcome is Outcome.PARTIAL, (case["id"], score.rule)
        if is_stale:
            stale_items += 1
            for score in scores:
                assert (
                    score.true_positives,
                    score.false_positives,
                    score.false_negatives,
                ) == (0, len(result.assigned), len(result.gold.ror_ids)), (case["id"], score.rule)
    # renamed, merged, withdrawn, two-step chain, plus a dead id with a related
    # edge and one with a hierarchy edge beside the chain
    assert stale_items >= 6


def test_non_active_assigned_id_never_grounds_whatever_its_edge() -> None:
    # The verifier's gap: with grounding decided by the recorded relation, a
    # dead id with a hierarchy edge beside its chain grounded under the
    # hierarchy rules while one with a related edge did not, because the
    # priority order reports the hierarchy edge and the succession relation
    # respectively. Now the status decides and the edge type only labels.
    for assigned, gold, relation in (
        (SUDBURY, HEALTH_SCIENCES_NORTH, Relation.CHILD_OF_GOLD),
        (PENINSULA, PLYMOUTH, Relation.PREDECESSOR_OF_GOLD),
    ):
        assert not is_active(assigned, GOLDEN_GRAPH)
        result = score_item(
            GoldLabel(Decision.ACCEPTED, frozenset({gold})), [assigned], GOLDEN_GRAPH
        )
        assert result.relations == (PairRelation(assigned, gold, relation),)
        assert result.stale_assigned == frozenset({assigned})
        for rule in MatchRule:
            assert result.outcome(rule) is Outcome.STALE, (assigned, rule)
    # The same dead id against a gold its chain never reaches is wrong, not
    # stale, and the hierarchy edge still does not ground it.
    result = score_item(
        GoldLabel(Decision.ACCEPTED, frozenset({WESTON_GENERAL})), [WESTON], GOLDEN_GRAPH
    )
    assert result.relations == (PairRelation(WESTON, WESTON_GENERAL, Relation.PARENT_OF_GOLD),)
    assert result.stale_assigned == frozenset()
    for rule in MatchRule:
        assert result.outcome(rule) is Outcome.WRONG, rule
    # An id the release does not hold is not active in it either.
    result = score_item(
        GoldLabel(Decision.ACCEPTED, frozenset({OXFORD})),
        ["https://ror.org/0zzzzzz99"],
        GOLDEN_GRAPH,
    )
    assert result.stale_assigned == frozenset()
    for rule in MatchRule:
        assert result.outcome(rule) is Outcome.WRONG, rule


def test_live_predecessor_or_successor_of_gold_is_wrong_not_stale() -> None:
    # Two of the 16 active records in v2.13 joined by a succession edge:
    # nothing is dead, so neither direction is stale, and with no hierarchy
    # edge nothing grounds. Both gold ids are active, so neither is refused.
    for assigned, gold, relation in (
        (DLSU, DLS_INSTITUTE, Relation.PREDECESSOR_OF_GOLD),
        (DLS_INSTITUTE, DLSU, Relation.SUCCESSOR_OF_GOLD),
    ):
        assert is_active(assigned, GOLDEN_GRAPH) and is_active(gold, GOLDEN_GRAPH)
        assert relation_between(assigned, gold, GOLDEN_GRAPH) is relation
        result = score_item(
            GoldLabel(Decision.ACCEPTED, frozenset({gold})), [assigned], GOLDEN_GRAPH
        )
        assert result.stale_assigned == frozenset()
        for rule in MatchRule:
            assert result.outcome(rule) is Outcome.WRONG, (assigned, rule)
    # A live predecessor beside the correct id is over-assigned under the
    # hierarchy rules only if an edge grounds it, and here none does: partial.
    gold = GoldLabel(Decision.ACCEPTED, frozenset({DLS_INSTITUTE}))
    result = score_item(gold, [DLSU, DLS_INSTITUTE], GOLDEN_GRAPH)
    for rule in MatchRule:
        assert result.outcome(rule) is Outcome.PARTIAL, rule
        assert result.by_rule[rule].false_positives == 1, rule


def test_succession_relations_satisfy_no_rule() -> None:
    for relation in SUCCESSION_RELATIONS:
        for rule in MatchRule:
            assert not PairRelation(KYUSHU_TOKAI, TOKAI, relation).satisfies(rule), (relation, rule)
    # And the relations that do ground something under the widest rule are
    # exactly the one-hop hierarchy and related edges plus identity.
    grounding = {
        relation
        for relation in Relation
        if PairRelation(OXFORD, KENNEDY, relation).satisfies(MatchRule.ANY_RELATIONSHIP)
    }
    assert grounding == {
        Relation.EXACT,
        Relation.PARENT_OF_GOLD,
        Relation.CHILD_OF_GOLD,
        Relation.RELATED,
    }


def test_no_assignment_is_never_wrong_and_wrong_is_never_no_assignment() -> None:
    for case in SCORED_CASES:
        result = score_item(gold_from_case(case), case["assigned"], GOLDEN_GRAPH)
        for score in result.by_rule.values():
            if not result.scorable:
                continue
            assert (score.outcome is Outcome.NO_ASSIGNMENT) == (not result.has_assignment)


# --- the dump loader as the graph -------------------------------------------


@pytest.fixture
def dump() -> Iterator[RorDump]:
    with RorDump.load(DUMP_SAMPLE, verify=False) as loaded:
        yield loaded


def test_dump_gives_the_same_answers_as_the_golden_graph(dump: RorDump) -> None:
    # Only cases whose gold ids the 14-record sample knows: a gold id outside
    # the release is an error by design, not a different answer. Likewise
    # only cases whose assigned ids the sample either holds or, like the
    # golden graph, does not hold at all: an id the golden graph holds as
    # active but the sample lacks (the Hokkaido hospital, the Kennedy
    # Institute, Weston) cannot ground in the sample, because a graph that
    # does not hold a record cannot know it is active, so the two graphs are
    # entitled to different answers there. That is the status rule working,
    # not a disagreement between the loader and the static graph.
    checked = refused = unanswerable = 0
    for case in GOLDEN_CASES:
        gold = gold_from_case(case)
        if not gold.ror_ids <= dump.ror_ids():
            continue
        assigned_ids = {normalise_ror_id(value) for value in case["assigned"]}
        if any(a in GOLDEN_GRAPH and a not in dump.ror_ids() for a in assigned_ids):
            unanswerable += 1
            continue
        if "expected_error" in case:
            # The sample dump holds the inactive gold record too, so the dump
            # must refuse the item the same way the static graph does.
            for graph in (dump, GOLDEN_GRAPH):
                with pytest.raises(expected_error(case)) as raised:
                    score_item(gold, case["assigned"], graph)
                for mention in case["expected_error"]["mentions"]:
                    assert mention in str(raised.value), (case["id"], mention)
            refused += 1
            continue
        from_dump = score_item(gold, case["assigned"], dump)
        from_static = score_item(gold, case["assigned"], GOLDEN_GRAPH)
        assert from_dump.by_rule == from_static.by_rule, case["id"]
        assert from_dump.relations == from_static.relations, case["id"]
        assert from_dump.stale_assigned == from_static.stale_assigned, case["id"]
        checked += 1
    assert checked >= 15
    assert refused >= 1
    assert unanswerable >= 2


def test_relation_lookup_uses_whichever_end_asserts_the_edge() -> None:
    # Kennedy asserts the parent edge; Oxford asserts the child edge. Drop each
    # end in turn and the relation must survive.
    only_child_end = StaticRorGraph(
        {
            OXFORD: GraphRecord("active", RelationshipSets(child=frozenset({KENNEDY}))),
            KENNEDY: GraphRecord("active"),
        }
    )
    only_parent_end = StaticRorGraph(
        {
            OXFORD: GraphRecord("active"),
            KENNEDY: GraphRecord("active", RelationshipSets(parent=frozenset({OXFORD}))),
        }
    )
    for graph in (only_child_end, only_parent_end):
        assert relation_between(KENNEDY, OXFORD, graph) is Relation.CHILD_OF_GOLD
        assert relation_between(OXFORD, KENNEDY, graph) is Relation.PARENT_OF_GOLD


def test_hierarchy_edge_outranks_a_related_edge_between_the_same_pair() -> None:
    graph = StaticRorGraph(
        {
            OXFORD: GraphRecord(
                "active",
                RelationshipSets(child=frozenset({KENNEDY}), related=frozenset({KENNEDY})),
            ),
            KENNEDY: GraphRecord("active"),
        }
    )
    assert relation_between(KENNEDY, OXFORD, graph) is Relation.CHILD_OF_GOLD


# --- successor chains ----------------------------------------------------------


def test_two_step_successor_chain_is_stale_in_the_golden_graph_and_the_dump(dump: RorDump) -> None:
    # Weston -> UHBW -> Bristol NHS FT, verified in the v2.13 dump. The sample
    # dump holds UHBW (which names Weston as predecessor) and Bristol, so the
    # chain is reconstructible there too even though Weston has no record.
    for graph in (GOLDEN_GRAPH, dump):
        assert relation_between(WESTON, BRISTOL_NHS_FT, graph) is Relation.PREDECESSOR_OF_GOLD
        assert succession_index_for(graph).steps(WESTON, BRISTOL_NHS_FT) == 2
        assert succession_index_for(graph).steps(UHBW, BRISTOL_NHS_FT) == 1
        gold = GoldLabel(Decision.ACCEPTED, frozenset({BRISTOL_NHS_FT}))
        result = score_item(gold, [WESTON], graph)
        for rule in MatchRule:
            assert result.outcome(rule) is Outcome.STALE, rule


def test_successor_of_gold_follows_the_chain_in_the_other_direction() -> None:
    # The reverse reading of the same chain: the live record assigned where the
    # gold names the twice-superseded one. Same chain, same closure. The
    # relation is computable for diagnostics, but a gold standard that names
    # the dead record is refused by the scorer (gate decision 1).
    assert relation_between(BRISTOL_NHS_FT, WESTON, GOLDEN_GRAPH) is Relation.SUCCESSOR_OF_GOLD
    assert succession_index_for(GOLDEN_GRAPH).steps(BRISTOL_NHS_FT, WESTON) is None
    gold = GoldLabel(Decision.ACCEPTED, frozenset({WESTON}))
    with pytest.raises(ValueError, match="must name the active record"):
        score_item(gold, [BRISTOL_NHS_FT], GOLDEN_GRAPH)


def test_non_active_gold_id_stops_the_run_naming_status_and_successors() -> None:
    # Weston is inactive with UHBW as its direct successor; the withdrawn PLOS
    # record names the active PLOS record. Both statuses are refused, the
    # message names the id, the status and the successor, and the check runs
    # before any relation is computed, so an empty assignment is refused too.
    for gold_id, status, successor in (
        (WESTON, "inactive", UHBW),
        ("https://ror.org/03bdvdc06", "withdrawn", "https://ror.org/008zgvp64"),
    ):
        gold = GoldLabel(Decision.ACCEPTED, frozenset({gold_id}))
        for assigned in ([OXFORD], []):
            with pytest.raises(ValueError) as raised:
                score_item(gold, assigned, GOLDEN_GRAPH)
            message = str(raised.value)
            assert gold_id in message and status in message and successor in message, message
    # One dead id in a multi-affiliation gold set is enough.
    gold = GoldLabel(Decision.ACCEPTED, frozenset({OXFORD, KYUSHU_TOKAI}))
    with pytest.raises(ValueError, match=KYUSHU_TOKAI):
        score_item(gold, [OXFORD], GOLDEN_GRAPH)
    # A dead id with no recorded successor still names its status.
    orphan = "https://ror.org/00000aa99"  # well-formed, not in the graph below as anything else
    graph = StaticRorGraph({orphan: GraphRecord("inactive"), OXFORD: GraphRecord("active")})
    with pytest.raises(ValueError, match="no recorded successor"):
        score_item(GoldLabel(Decision.ACCEPTED, frozenset({orphan})), [OXFORD], graph)


def test_succession_index_uses_whichever_end_asserts_each_edge() -> None:
    # X -> Y asserted only on Y (predecessor), Y -> Z asserted only on Y
    # (successor): neither end of the chain says anything, the middle says both.
    x, y, z = "https://ror.org/0000000x1", "https://ror.org/0000000y2", "https://ror.org/0000000z3"
    graph = StaticRorGraph(
        {
            x: GraphRecord("inactive"),
            y: GraphRecord(
                "inactive",
                RelationshipSets(predecessor=frozenset({x}), successor=frozenset({z})),
            ),
            z: GraphRecord("active"),
        }
    )
    index = SuccessionIndex(graph)
    assert index.successors_of(x) == frozenset({y})
    assert index.successors_of(y) == frozenset({z})
    assert index.steps(x, z) == 2
    assert index.supersedes(x, z)
    assert not index.supersedes(z, x)
    assert relation_between(x, z, graph) is Relation.PREDECESSOR_OF_GOLD
    assert relation_between(z, x, graph) is Relation.SUCCESSOR_OF_GOLD


def test_succession_index_terminates_on_a_cycle_and_never_makes_an_id_its_own_predecessor() -> None:
    a, b, c = "https://ror.org/0000000a1", "https://ror.org/0000000b2", "https://ror.org/0000000c3"
    graph = StaticRorGraph(
        {
            a: GraphRecord("inactive", RelationshipSets(successor=frozenset({b}))),
            b: GraphRecord("inactive", RelationshipSets(successor=frozenset({a}))),
            c: GraphRecord("active"),
        }
    )
    index = SuccessionIndex(graph)
    assert index.steps(a, b) == 1
    assert index.steps(b, a) == 1
    assert index.steps(a, c) is None
    assert index.steps(a, a) is None
    assert relation_between(a, c, graph) is Relation.NONE


def test_succession_index_is_built_once_per_graph_object() -> None:
    other = StaticRorGraph({OXFORD: GraphRecord("active")})
    assert succession_index_for(GOLDEN_GRAPH) is succession_index_for(GOLDEN_GRAPH)
    assert succession_index_for(other) is succession_index_for(other)
    assert succession_index_for(other) is not succession_index_for(GOLDEN_GRAPH)


def test_direct_successor_edge_still_outranks_a_related_edge() -> None:
    graph = StaticRorGraph(
        {
            KYUSHU_TOKAI: GraphRecord(
                "inactive",
                RelationshipSets(successor=frozenset({TOKAI}), related=frozenset({TOKAI})),
            ),
            TOKAI: GraphRecord("active"),
        }
    )
    assert relation_between(KYUSHU_TOKAI, TOKAI, graph) is Relation.PREDECESSOR_OF_GOLD


# --- element counts and coverage ------------------------------------------------


def test_rule_score_refuses_counts_that_disagree_with_the_outcome() -> None:
    with pytest.raises(ValueError, match="must not carry element counts"):
        RuleScore(MatchRule.EXACT, Outcome.NO_ASSIGNMENT, 0, 0, 1)
    with pytest.raises(ValueError, match="must not carry element counts"):
        RuleScore(MatchRule.EXACT, Outcome.GOLD_AMBIGUOUS, None, 0, None)
    with pytest.raises(ValueError, match="must carry element counts"):
        RuleScore(MatchRule.EXACT, Outcome.WRONG, None, None, None)
    with pytest.raises(ValueError, match="must carry element counts"):
        RuleScore(MatchRule.EXACT, Outcome.CORRECT, 1, 0, None)
    # Gate decision 2: a no_ror gold carries counts only as false positives.
    assert RuleScore(MatchRule.EXACT, Outcome.GOLD_NO_ROR, None, None, None).counted is False
    assert RuleScore(MatchRule.EXACT, Outcome.GOLD_NO_ROR, 0, 2, 0).counted is True
    for counts in ((1, 1, 0), (0, 1, 1), (0, 0, 0)):
        with pytest.raises(ValueError, match="only false positives"):
            RuleScore(MatchRule.EXACT, Outcome.GOLD_NO_ROR, *counts)
    with pytest.raises(ValueError, match="all three counts or none"):
        RuleScore(MatchRule.EXACT, Outcome.GOLD_NO_ROR, None, 1, None)


def test_no_ror_assignment_costs_precision_but_not_recall() -> None:
    # Gate decision 2 in micro-averaged form: one exact hit, one invented
    # affiliation on a no_ror item, one correct abstention on a no_ror item.
    # Precision is 1 of 2, recall is 1 of 1, and only two items are counted.
    by_id = {case["id"]: case for case in GOLDEN_CASES}
    results = [
        score_item(gold_from_case(by_id[case_id]), by_id[case_id]["assigned"], GOLDEN_GRAPH)
        for case_id in (
            "exact_hit",
            "gold_no_ror_with_assignment",
            "gold_no_ror_without_assignment",
        )
    ]
    for rule in MatchRule:
        counts = sum_element_counts(rule, (r.by_rule[rule] for r in results))
        assert counts == ElementCounts(rule, 1, 1, 0, 2)
    # Neither no_ror item is in the coverage denominator: abstaining where no
    # ROR record exists is not a coverage failure.
    assert [r.scorable for r in results] == [True, False, False]
    assert [r.outcome(MatchRule.EXACT) for r in results[1:]] == [Outcome.GOLD_NO_ROR] * 2


def test_abstentions_never_reach_a_recall_denominator() -> None:
    # The auditor's concrete case: a source that is exactly right on every
    # item it assigns and silent on the rest. Recall over assigned items is
    # whole; coverage is where the silence shows.
    by_id = {case["id"]: case for case in GOLDEN_CASES}
    exact = by_id["exact_hit"]
    hit = score_item(gold_from_case(exact), exact["assigned"], GOLDEN_GRAPH)
    silent = by_id["empty_assignment_multi_affiliation"]
    abstained = score_item(gold_from_case(silent), silent["assigned"], GOLDEN_GRAPH)
    results = [hit, abstained, abstained]
    for rule in MatchRule:
        counts = sum_element_counts(rule, (r.by_rule[rule] for r in results))
        assert counts == ElementCounts(rule, 1, 0, 0, 1)
    assert sum(1 for r in results if r.has_assignment) == 1
    assert sum(1 for r in results if r.scorable) == 3


def test_unscorable_gold_is_skipped_and_mixed_rules_are_refused() -> None:
    by_id = {case["id"]: case for case in GOLDEN_CASES}
    ambiguous = by_id["gold_ambiguous_with_assignment"]
    unscored = score_item(gold_from_case(ambiguous), ambiguous["assigned"], GOLDEN_GRAPH)
    partial = by_id["partial_one_right_one_wrong"]
    scored = score_item(gold_from_case(partial), partial["assigned"], GOLDEN_GRAPH)
    counts = sum_element_counts(
        MatchRule.EXACT, [unscored.by_rule[MatchRule.EXACT], scored.by_rule[MatchRule.EXACT]]
    )
    assert counts == ElementCounts(MatchRule.EXACT, 1, 1, 1, 1)
    assert sum_element_counts(MatchRule.EXACT, []) == ElementCounts(MatchRule.EXACT, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="cannot sum a parent_child score"):
        sum_element_counts(MatchRule.EXACT, [scored.by_rule[MatchRule.PARENT_CHILD]])


# --- ancestry diagnostic ------------------------------------------------------


def test_ancestry_refuses_the_up_and_down_path(dump: RorDump) -> None:
    # Oxford and the trust both list the CRUK Oxford Centre as a child, so an
    # undirected walk joins the John Radcliffe to Oxford in three steps
    # (hospital, trust, centre, university). That is the degenerate closure the
    # diagnostic exists to exclude.
    assert CRUK_OXFORD_CENTRE in dump.relationships(OXFORD).child
    assert CRUK_OXFORD_CENTRE in dump.relationships(OUH_TRUST).child
    index = AncestryIndex(dump)
    assert index.distance(JOHN_RADCLIFFE, OXFORD, max_depth=10) is None
    assert index.distance(CRUK_OXFORD_CENTRE, OXFORD) == 1
    assert index.distance(CRUK_OXFORD_CENTRE, OUH_TRUST) == 1


def test_ancestry_climbs_through_ids_with_no_record_of_their_own(dump: RorDump) -> None:
    # The Institute of Physics names CAS as parent, but CAS is not in the
    # 14-record sample. The edge is still an edge.
    index = AncestryIndex(dump)
    assert index.distance(IOP, CAS) == 1
    assert index.distance(CAS, IOP) == 1
    # And the reverse index: Oxford lists a child that has no record here.
    assert index.distance(CRUK_MRC_INSTITUTE, OXFORD) == 1


def test_ancestry_two_hops_and_depth_limit() -> None:
    index = AncestryIndex(GOLDEN_GRAPH)
    assert index.distance(SKLM, CAS) == 2
    assert index.distance(CAS, SKLM) == 2
    assert index.distance(SKLM, CAS, max_depth=1) is None
    assert index.distance(SKLM, SKLM, max_depth=0) == 0
    assert index.distance(SKLM, IOP, max_depth=0) is None
    assert index.min_distance([], [CAS]) is None
    assert index.min_distance([SKLM, HOKKAIDO], [CAS, OXFORD]) == 2
    with pytest.raises(ValueError, match="max_depth"):
        index.distance(SKLM, CAS, max_depth=-1)


def test_parent_child_rule_is_exactly_ancestry_distance_at_most_one_for_a_live_id() -> None:
    # The diagnostic is structural and knows nothing about status, so the
    # equivalence holds for live assigned ids. A dead id at distance 1 is
    # STALE or WRONG, never a hit, and the golden file has one of each.
    index = AncestryIndex(GOLDEN_GRAPH)
    dead_at_distance_one = 0
    for case in SCORED_CASES:
        gold = gold_from_case(case)
        if not gold.resolved or len(gold.ror_ids) != 1 or len(case["assigned"]) != 1:
            continue
        result = score_item(gold, case["assigned"], GOLDEN_GRAPH)
        (assigned,) = result.assigned
        distance = index.min_distance(case["assigned"], gold.ror_ids)
        is_hit = result.outcome(MatchRule.PARENT_CHILD) is Outcome.CORRECT
        within_one = distance is not None and distance <= 1
        assert is_hit == (within_one and is_active(assigned, GOLDEN_GRAPH)), case["id"]
        if within_one and not is_active(assigned, GOLDEN_GRAPH):
            dead_at_distance_one += 1
            assert result.outcome(MatchRule.PARENT_CHILD) in {Outcome.STALE, Outcome.WRONG}
    assert dead_at_distance_one >= 2


# --- refusing bad input --------------------------------------------------------


def test_gold_id_outside_the_release_stops_the_run() -> None:
    gold = GoldLabel(Decision.ACCEPTED, frozenset({"https://ror.org/0zzzzzz99"}))
    with pytest.raises(ValueError, match="not in the pinned ROR release"):
        score_item(gold, [OXFORD], GOLDEN_GRAPH)
    # The gold side is checked before any pair is formed, so an abstention
    # against an unknown gold id is refused rather than scored NO_ASSIGNMENT.
    with pytest.raises(ValueError, match="not in the pinned ROR release"):
        score_item(gold, [], GOLDEN_GRAPH)


def test_skipped_decision_cannot_be_a_gold_label() -> None:
    with pytest.raises(ValueError, match="skipped"):
        GoldLabel(Decision.SKIPPED)


def test_resolved_gold_needs_ids_and_unresolved_gold_refuses_them() -> None:
    with pytest.raises(ValueError, match="at least one ROR id"):
        GoldLabel(Decision.ACCEPTED)
    with pytest.raises(ValueError, match="must not carry ROR ids"):
        GoldLabel(Decision.AMBIGUOUS, frozenset({OXFORD}))
    with pytest.raises(ValueError, match="must not carry ROR ids"):
        GoldLabel(Decision.NO_ROR, frozenset({OXFORD}))


def test_gold_label_from_a_review_label() -> None:
    label = Label(
        item_id="item-1",
        annotator="a",
        decision=Decision.CORRECTED,
        ror_ids=["052gg0110", OXFORD],
        elapsed_ms=1,
        submitted_at="2026-10-01T00:00:00+00:00",
    )
    gold = GoldLabel.from_label(label)
    assert gold.ror_ids == frozenset({OXFORD})
    assert gold.resolved


@pytest.mark.parametrize(
    "value",
    [
        "https://ror.org/052gg0110",
        "http://ror.org/052gg0110",
        "ror.org/052gg0110",
        "052gg0110",
        "  052GG0110  ",
        "HTTPS://ROR.ORG/052gg0110",
    ],
)
def test_normalise_accepts_every_spelling_of_the_same_id(value: str) -> None:
    assert normalise_ror_id(value) == OXFORD


@pytest.mark.parametrize(
    "value",
    [
        "",
        "grid.4991.5",
        "https://ror.org/",
        "https://ror.org/152gg0110",  # must start with 0
        "https://ror.org/052gg011",  # too short
        "https://ror.org/05ilou110",  # i, l, o, u are not in the alphabet
        "https://ror.org/052gg01x0",  # checksum digits must be digits
        "https://openalex.org/I40120149",
    ],
)
def test_normalise_rejects_anything_not_shaped_like_a_ror_id(value: str) -> None:
    with pytest.raises(ValueError, match="not shaped like a ROR id"):
        normalise_ror_id(value)


def test_malformed_assigned_id_is_a_pipeline_error_not_a_miss() -> None:
    gold = GoldLabel(Decision.ACCEPTED, frozenset({OXFORD}))
    with pytest.raises(ValueError, match="not shaped like a ROR id"):
        score_item(gold, ["grid.4991.5"], GOLDEN_GRAPH)


def test_duplicate_spellings_in_an_assignment_collapse_to_one_id() -> None:
    gold = GoldLabel(Decision.ACCEPTED, frozenset({OXFORD}))
    result = score_item(gold, [OXFORD, "052gg0110"], GOLDEN_GRAPH)
    assert result.assigned == frozenset({OXFORD})
    assert result.outcome(MatchRule.EXACT) is Outcome.CORRECT


# --- structural fairness ----------------------------------------------------------


def test_scoring_function_cannot_be_told_which_source_it_scores() -> None:
    assert list(inspect.signature(score_item).parameters) == ["gold", "assigned", "graph"]
    assert list(inspect.signature(relation_between).parameters) == ["assigned", "gold", "graph"]


EVALUATION_PATH_MODULES = (
    "src/disambig/matching.py",
    "src/disambig/ror_ids.py",
    "src/disambig/ror_dump.py",
    "src/disambig/findings.py",
    "src/disambig/provenance.py",
    "src/disambig/snapshot.py",
    "scripts/render_post.py",
    "scripts/hash_snapshot.py",
)


@pytest.mark.parametrize("relative", EVALUATION_PATH_MODULES)
def test_evaluation_path_never_names_an_evaluated_source(relative: str) -> None:
    """Source neutrality is structural: nothing between a harvested assignment
    and a published number may know which source it is looking at. A grep of
    one module is not enough, so every module on that path is checked."""
    project = Path(inspect.getfile(matching)).resolve().parents[2]
    source = (project / relative).read_text().lower()
    for name in ("dimensions", "openalex", "crossref", "pubmed", "source_a", "source_b"):
        assert not re.search(rf"\b{name}\b", source), f"{relative} names {name}"
