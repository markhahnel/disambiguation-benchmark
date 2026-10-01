"""The three ROR matching rules, the set-level scoring on top of them, and the
ancestry-distance diagnostic. This is the module the fairness of Benchmark A
rests on, so the definitions are spelled out here in full and the tests pin
every one of them to real records from the pinned ROR release.

Why three rules. A large share of apparent disambiguation errors are hierarchy
choices rather than errors: a hospital that is a ROR child of a university, an
institute inside a national research council. Whether "Institute of Physics"
assigned to an author who wrote "Chinese Academy of Sciences" is right depends
on a decision the benchmark has no business making on the reader's behalf. So
every item is scored under all three rules and all three are published, with no
primary rule, because the choice can flip the ranking of sources.

The rules, each applied to one assigned id and one gold id:

  EXACT             the assigned id equals the gold id.
  PARENT_CHILD      exact, or the two ids are joined by a single direct
                    parent/child edge in either direction.
  ANY_RELATIONSHIP  exact, or the two ids are joined by a single direct edge
                    of any ROR type (parent, child, related, predecessor,
                    successor) in either direction, where "predecessor" and
                    "successor" are read along the whole successor chain (see
                    "Successor chains" below).

Hierarchy and related edges are one hop by definition. Transitive closure over
them is deliberately not a headline rule: closure over "related" edges is
degenerate (it would eventually connect most of a national academy's estate,
and university hospitals to every university they share a joint centre with),
which would make the rule meaningless. Closure over the hierarchy alone is
offered instead as a separate diagnostic, :func:`AncestryIndex.distance`, the
shortest monotone path over parent/child edges with a maximum depth, to be
reported as a sensitivity analysis rather than as a fourth rule.

Successor chains. Predecessor and successor edges are the one place closure is
taken, and the reason is what the edges mean. A related edge asserts
association and a parent edge asserts membership, and neither is transitive in
any useful sense. A successor edge asserts identity over time: the new record
is the same organisation under a new name or after a merger. Chains of them
are directed, short and acyclic (a registry that twice renames or twice merges
an organisation produces a two-step chain), so following them cannot connect
anything that was not already the same institution. An assigned id is
therefore a PREDECESSOR_OF_GOLD if a chain of one or more successor edges
leads from it to the gold id, and a SUCCESSOR_OF_GOLD if such a chain leads
from the gold id to it. :class:`SuccessionIndex` holds the forward adjacency,
built once per release from whichever end asserts each edge, and the walk
keeps a seen set so a curation error that closes a loop terminates rather than
hangs.

An edge counts if either end asserts it. ROR usually records hierarchy and
succession from both ends but does not always, and a match must not depend on
which end the registry happened to write down.

Element relation versus rule outcome. For every (assigned, gold) pair the
rule-independent :class:`Relation` is recorded alongside the per-rule
outcomes, so the error taxonomy (stale id, hierarchy choice, related
organisation, plainly wrong) can be tabulated from the relations without
re-running anything, and without it depending on which rule a reader prefers.
Where a pair has edges of more than one type, the relation reported is the
first in this order: exact, parent, child, predecessor, successor, related.

Stale ids. A source that assigns a superseded id whose successor chain leads
to the gold id (a renamed or merged institution, however many times) gets the
distinct outcome STALE under EXACT and PARENT_CHILD, never a plain miss,
because it is one of the error-taxonomy categories in the brief. Under
ANY_RELATIONSHIP the successor chain is a qualifying relation, so by the
rule's own definition the pair is a hit. That is a consequence of the design
decision above, not a separate choice, and it means the stale category is
only visible under the first two rules and in the relations.

Set-level scoring. Gold labels can be sets (multi-affiliation items) and so can
assignments. Let G be the gold set, A the assigned set, and M the size of a
maximum bipartite matching between A and G where a pair may be matched if it
satisfies the rule. An assigned id is "grounded" if it satisfies the rule with
at least one gold id. Then, in order:

  NO_ASSIGNMENT  A is empty. A coverage failure, never an accuracy failure: a
                 source that assigns nothing is perfectly precise and useless,
                 so this outcome is never folded into WRONG and carries no
                 element counts (see "Element counts" below).
  CORRECT        every assigned id is grounded and M == |A| == |G|. Under EXACT
                 this is set equality.
  COLLAPSED      every assigned id is grounded but M < |G|: the source lost at
                 least one of the author's affiliations and asserted nothing
                 wrong. This is the multi-affiliation collapse the brief asks
                 to keep distinguishable from a wrong assignment. It is also
                 what a single parent id scores against a gold set of {parent,
                 child} under the hierarchy rules: one assigned id can ground
                 against two gold ids but can only be matched to one.
  OVER_ASSIGNED  every assigned id is grounded, M == |G|, and |A| > |G|: the
                 source listed more ids than the author did, all of them in
                 the gold ids' one-hop neighbourhood (for example both a
                 university and its institute where the author wrote the
                 university). Impossible under EXACT.
  STALE          no assigned id is grounded (M == 0) and every assigned id is
                 the predecessor of some gold id. The whole assignment is the
                 right institution under old identifiers and nothing else.
  PARTIAL        at least one assigned id is ungrounded and M > 0: some of the
                 assignment is right and some of it is wrong. This includes an
                 assignment that mixes correct ids with a stale duplicate or a
                 stale sibling; the predecessor relation is still recorded for
                 that pair, so the stale id stays countable in the error
                 taxonomy without the item being labelled "stale id" when
                 half of it was right. (Under ANY_RELATIONSHIP the same
                 assignment is OVER_ASSIGNED or CORRECT, since the stale id
                 is grounded there.)
  WRONG          at least one assigned id is ungrounded and M == 0.

Element counts. Every outcome in CORRECT, COLLAPSED, OVER_ASSIGNED, STALE,
PARTIAL and WRONG carries true_positives = M, false_positives = |A| - M and
false_negatives = |G| - M. NO_ASSIGNMENT, GOLD_AMBIGUOUS and GOLD_NO_ROR carry
None for all three. Accuracy metrics (precision, recall, F1) are therefore
computed over assigned, scorable items only: :func:`sum_element_counts` sums
the counts of exactly those items and refuses to mix rules. Coverage, the
share of scorable items on which a source assigned anything at all
(:attr:`MatchResult.has_assignment`), is the complementary published figure
and is always reported next to accuracy. Folding abstentions into recall by
giving them false negatives would let two analysts publish two different
recall figures for the same source; the None values make that impossible to
do by accident.

Ambiguous and no_ror gold labels are first-class labels, not failures to label.
An item whose gold decision is ambiguous scores GOLD_AMBIGUOUS and one whose
gold decision is no_ror scores GOLD_NO_ROR, under every rule, whatever the
source assigned, with no element counts. Neither counts as correct or
incorrect anywhere in this module. The assignment is still recorded on the
result, so the rate at which a source assigns something to an item the
annotator judged to have no ROR can be reported separately.

Gold ids outside the release. :func:`relation_between` raises ValueError when a
gold id is absent from the pinned release, and this module keeps doing so on
purpose. The remedy belongs at labelling time: the review app refuses ids
outside the pinned release, so a frozen gold standard can never name an
organisation the scorer cannot see. An error here therefore means that guard
was bypassed, which is a gold-standard integrity problem, not a source error,
and silently scoring around it would launder a broken gold standard into a
published number.

Pending gate decisions. The following are implemented one way for now and
listed here so they are not mistaken for settled design. Each is a project
owner decision and changing it is a change to the stated design, applied to
every source equally:

  1. Whether ANY_RELATIONSHIP should exclude predecessor and successor edges
     so that stale ids stay a visible error under all three rules. Currently
     they count as hits under that rule.
  2. Whether an assignment made against a no_ror gold item is a precision
     failure. Currently it scores GOLD_NO_ROR and is reported as its own
     rate only; treating it as WRONG would penalise sources that assign more
     aggressively, not treating it so lets a source invent affiliations at no
     cost.
  3. Whether OVER_ASSIGNED counts as a hit under the hierarchy rules.
     Currently it does not, and the extra ids count as false positives; it is
     a separate outcome so either reading can be computed.
  4. The maximum depth the ancestry-distance diagnostic is reported at.
     :data:`DEFAULT_ANCESTRY_MAX_DEPTH` is a placeholder chosen without data.

What this module refuses to do. The scoring function takes a gold label, an
assigned set and the ROR graph. It does not take a source identifier, and
there is no per-source branch anywhere in it. The author works for the maker of
one of the evaluated sources, and the counter-pressure has to live in the
code: handling needed by one source is applied to all of them or to none.
"""

from __future__ import annotations

import weakref
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

import structlog

from disambig.models import Decision, Label
from disambig.ror_dump import NO_RELATIONSHIPS, RelationshipSets
from disambig.ror_ids import normalise_ror_id, normalise_ror_ids

log = structlog.get_logger(__name__)

DEFAULT_ANCESTRY_MAX_DEPTH = 5
RESOLVED_DECISIONS = frozenset({Decision.ACCEPTED, Decision.CORRECTED})


class RorGraph(Protocol):
    """What matching needs from a ROR release. RorDump satisfies it structurally."""

    def __contains__(self, ror_id: str) -> bool: ...

    def status(self, ror_id: str) -> str: ...

    def relationships(self, ror_id: str) -> RelationshipSets: ...

    @property
    def relationship_index(self) -> Mapping[str, RelationshipSets]: ...


@dataclass(frozen=True)
class GraphRecord:
    """One organisation in a :class:`StaticRorGraph`."""

    status: str
    relationships: RelationshipSets = NO_RELATIONSHIPS


class StaticRorGraph:
    """A RorGraph over an explicit mapping, for fixtures and extracted subgraphs.

    Sparse like the dump index: an edge may name an id with no record of its
    own, which is how a hand-extracted subgraph of the real release looks.
    """

    def __init__(self, records: Mapping[str, GraphRecord]) -> None:
        self._records = dict(records)
        self._index: dict[str, RelationshipSets] = {
            ror_id: record.relationships
            for ror_id, record in records.items()
            if record.relationships.all_ids()
        }

    def __contains__(self, ror_id: str) -> bool:
        return ror_id in self._records

    def __len__(self) -> int:
        return len(self._records)

    def status(self, ror_id: str) -> str:
        record = self._records.get(ror_id)
        if record is None:
            raise KeyError(f"{ror_id} is not in the graph")
        return record.status

    def relationships(self, ror_id: str) -> RelationshipSets:
        record = self._records.get(ror_id)
        if record is None:
            raise KeyError(f"{ror_id} is not in the graph")
        return record.relationships

    @property
    def relationship_index(self) -> Mapping[str, RelationshipSets]:
        return self._index


class MatchRule(StrEnum):
    EXACT = "exact"
    PARENT_CHILD = "parent_child"
    ANY_RELATIONSHIP = "any_relationship"


class Relation(StrEnum):
    """How one assigned id stands to one gold id in the pinned release.

    Rule-independent: this is what the error taxonomy is built from.
    """

    EXACT = "exact"
    PARENT_OF_GOLD = "parent_of_gold"  # the assigned id is the gold id's parent
    CHILD_OF_GOLD = "child_of_gold"  # the assigned id is the gold id's child
    PREDECESSOR_OF_GOLD = "predecessor_of_gold"  # stale: a successor chain leads to the gold id
    SUCCESSOR_OF_GOLD = "successor_of_gold"  # a successor chain leads from the gold id
    RELATED = "related"
    NONE = "none"  # both ids are in the release, no direct edge joins them
    NOT_IN_DUMP = "not_in_dump"  # the assigned id is absent from the pinned release


# Which relations satisfy which rule. The STALE outcome depends on
# PREDECESSOR_OF_GOLD being absent from PARENT_CHILD; see the module docstring.
_RULE_RELATIONS: Mapping[MatchRule, frozenset[Relation]] = {
    MatchRule.EXACT: frozenset({Relation.EXACT}),
    MatchRule.PARENT_CHILD: frozenset(
        {Relation.EXACT, Relation.PARENT_OF_GOLD, Relation.CHILD_OF_GOLD}
    ),
    MatchRule.ANY_RELATIONSHIP: frozenset(
        {
            Relation.EXACT,
            Relation.PARENT_OF_GOLD,
            Relation.CHILD_OF_GOLD,
            Relation.PREDECESSOR_OF_GOLD,
            Relation.SUCCESSOR_OF_GOLD,
            Relation.RELATED,
        }
    ),
}


class Outcome(StrEnum):
    CORRECT = "correct"
    COLLAPSED = "collapsed"
    OVER_ASSIGNED = "over_assigned"
    STALE = "stale"
    PARTIAL = "partial"
    WRONG = "wrong"
    NO_ASSIGNMENT = "no_assignment"
    GOLD_AMBIGUOUS = "gold_ambiguous"
    GOLD_NO_ROR = "gold_no_ror"


# The gold side cannot be scored: these outcomes are neither hits nor misses.
UNSCORED_OUTCOMES = frozenset({Outcome.GOLD_AMBIGUOUS, Outcome.GOLD_NO_ROR})
# Outcomes that carry no element counts. NO_ASSIGNMENT is scorable (the gold
# is resolved) but is coverage, not accuracy; see the module docstring.
UNCOUNTED_OUTCOMES = UNSCORED_OUTCOMES | {Outcome.NO_ASSIGNMENT}


@dataclass(frozen=True)
class GoldLabel:
    """The adjudicated truth for one item, as the frozen gold standard holds it.

    ror_ids is non-empty exactly when the decision is accepted or corrected.
    """

    decision: Decision
    ror_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.decision is Decision.SKIPPED:
            raise ValueError("a skipped item has no gold label and cannot be scored")
        if self.decision in RESOLVED_DECISIONS and not self.ror_ids:
            raise ValueError(f"a {self.decision.value} gold label needs at least one ROR id")
        if self.decision not in RESOLVED_DECISIONS and self.ror_ids:
            raise ValueError(f"a {self.decision.value} gold label must not carry ROR ids")
        object.__setattr__(self, "ror_ids", normalise_ror_ids(self.ror_ids))

    @classmethod
    def from_label(cls, label: Label) -> GoldLabel:
        return cls(decision=label.decision, ror_ids=frozenset(label.ror_ids))

    @property
    def resolved(self) -> bool:
        return self.decision in RESOLVED_DECISIONS


@dataclass(frozen=True)
class PairRelation:
    assigned: str
    gold: str
    relation: Relation

    def satisfies(self, rule: MatchRule) -> bool:
        return self.relation in _RULE_RELATIONS[rule]


@dataclass(frozen=True)
class RuleScore:
    """One item under one rule.

    Counts are None exactly when the outcome is in UNCOUNTED_OUTCOMES: the
    gold side is unscorable, or nothing was assigned. The constructor enforces
    that, so a count can never be summed from an outcome that must not have
    one.
    """

    rule: MatchRule
    outcome: Outcome
    true_positives: int | None
    false_positives: int | None
    false_negatives: int | None

    def __post_init__(self) -> None:
        counts = (self.true_positives, self.false_positives, self.false_negatives)
        if self.counted and any(count is None for count in counts):
            raise ValueError(f"a {self.outcome.value} score must carry element counts")
        if not self.counted and any(count is not None for count in counts):
            raise ValueError(f"a {self.outcome.value} score must not carry element counts")

    @property
    def scorable(self) -> bool:
        """The gold side is resolved, so the item belongs in a coverage denominator."""
        return self.outcome not in UNSCORED_OUTCOMES

    @property
    def counted(self) -> bool:
        """The item carries element counts, so it belongs in an accuracy denominator."""
        return self.outcome not in UNCOUNTED_OUTCOMES

    @property
    def hit(self) -> bool:
        return self.outcome is Outcome.CORRECT


@dataclass(frozen=True)
class ElementCounts:
    """Summed element counts over the counted items of one rule, for micro-averaging."""

    rule: MatchRule
    true_positives: int
    false_positives: int
    false_negatives: int
    items: int  # how many scores contributed; abstentions and unscorable gold are not among them


def sum_element_counts(rule: MatchRule, scores: Iterable[RuleScore]) -> ElementCounts:
    """Sum element counts over assigned, scorable items under one rule.

    NO_ASSIGNMENT, GOLD_AMBIGUOUS and GOLD_NO_ROR scores are skipped, so an
    abstention never reaches a recall denominator here. Coverage is reported
    from :attr:`MatchResult.has_assignment` instead. A score under a different
    rule is an error, not something to average over.
    """
    true_positives = false_positives = false_negatives = items = 0
    for score in scores:
        if score.rule is not rule:
            raise ValueError(f"cannot sum a {score.rule.value} score into {rule.value} counts")
        if not score.counted:
            continue
        assert score.true_positives is not None
        assert score.false_positives is not None
        assert score.false_negatives is not None
        true_positives += score.true_positives
        false_positives += score.false_positives
        false_negatives += score.false_negatives
        items += 1
    return ElementCounts(rule, true_positives, false_positives, false_negatives, items)


@dataclass(frozen=True)
class MatchResult:
    gold: GoldLabel
    assigned: frozenset[str]
    relations: tuple[PairRelation, ...]
    by_rule: Mapping[MatchRule, RuleScore]

    @property
    def has_assignment(self) -> bool:
        """Coverage. Reported next to accuracy, never mixed into it."""
        return bool(self.assigned)

    @property
    def scorable(self) -> bool:
        return self.gold.resolved

    def outcome(self, rule: MatchRule) -> Outcome:
        return self.by_rule[rule].outcome


class SuccessionIndex:
    """Forward successor adjacency over one release, from whichever end asserts an edge.

    ROR records a rename or merger as a successor edge on the old record and a
    predecessor edge on the new one, but not always both, so the index is
    built once over the whole relationship index, the way
    :class:`AncestryIndex` builds its reverse index over child lists.
    :meth:`steps` walks the chain forwards from one id and reports how many
    successor edges it took to reach another, or None. The walk keeps a seen
    set so a curation error that closes a loop terminates. The module
    docstring explains why closure is taken over these edges and no others.
    """

    def __init__(self, graph: RorGraph) -> None:
        successors: dict[str, set[str]] = {}
        for ror_id, sets in graph.relationship_index.items():
            if sets.successor:
                successors.setdefault(ror_id, set()).update(sets.successor)
            for predecessor in sets.predecessor:
                successors.setdefault(predecessor, set()).add(ror_id)
        self._successors: dict[str, frozenset[str]] = {
            ror_id: frozenset(ids) for ror_id, ids in successors.items()
        }

    def successors_of(self, ror_id: str) -> frozenset[str]:
        return self._successors.get(ror_id, frozenset())

    def steps(self, old: str, new: str) -> int | None:
        """Length of the shortest successor chain from old to new, or None.

        Identity is not a chain: steps(x, x) is None, so a record is never
        its own predecessor.
        """
        if old == new:
            return None
        seen = {old}
        queue: deque[tuple[str, int]] = deque([(old, 0)])
        while queue:
            node, depth = queue.popleft()
            for successor in self.successors_of(node):
                if successor == new:
                    return depth + 1
                if successor not in seen:
                    seen.add(successor)
                    queue.append((successor, depth + 1))
        return None

    def supersedes(self, old: str, new: str) -> bool:
        """Whether a chain of one or more successor edges leads from old to new."""
        return self.steps(old, new) is not None


# One index per graph object, built on first use and released with the graph.
# Keyed weakly so a cached index can never be handed out for a different graph
# that happens to reuse the address of a dead one. Graphs are immutable after
# construction, which is what makes caching sound.
_SUCCESSION_INDEXES: weakref.WeakKeyDictionary[RorGraph, SuccessionIndex] = (
    weakref.WeakKeyDictionary()
)


def succession_index_for(graph: RorGraph) -> SuccessionIndex:
    """The :class:`SuccessionIndex` of a graph, built once per graph object."""
    try:
        return _SUCCESSION_INDEXES[graph]
    except KeyError:
        index = SuccessionIndex(graph)
        _SUCCESSION_INDEXES[graph] = index
        return index


def relation_between(assigned: str, gold: str, graph: RorGraph) -> Relation:
    """The rule-independent relation of one assigned id to one gold id.

    Both ids must already be normalised. The gold id must be in the release:
    a gold id the pinned dump does not know is a gold-standard integrity
    error, not a source error, and it stops the run. The review app refuses
    such ids at labelling time, which is where the remedy belongs (see the
    module docstring); this function does not score around it.

    Hierarchy and related edges are read one hop from either end. Predecessor
    and successor edges are read along the whole chain through
    :func:`succession_index_for`, which also covers the direct edge.
    """
    if gold not in graph:
        raise ValueError(f"gold ROR id {gold} is not in the pinned ROR release")
    if assigned == gold:
        return Relation.EXACT
    from_gold = graph.relationships(gold)
    from_assigned = graph.relationships(assigned) if assigned in graph else NO_RELATIONSHIPS
    if gold in from_assigned.child or assigned in from_gold.parent:
        return Relation.PARENT_OF_GOLD
    if gold in from_assigned.parent or assigned in from_gold.child:
        return Relation.CHILD_OF_GOLD
    succession = succession_index_for(graph)
    if succession.supersedes(assigned, gold):
        return Relation.PREDECESSOR_OF_GOLD
    if succession.supersedes(gold, assigned):
        return Relation.SUCCESSOR_OF_GOLD
    if gold in from_assigned.related or assigned in from_gold.related:
        return Relation.RELATED
    if assigned not in graph:
        return Relation.NOT_IN_DUMP
    return Relation.NONE


def _maximum_matching(
    assigned: tuple[str, ...], gold: tuple[str, ...], ok: set[tuple[str, str]]
) -> int:
    """Size of a maximum bipartite matching between assigned and gold ids.

    Sets here have a handful of elements, so the augmenting-path algorithm is
    plenty. Inputs are sorted tuples, so the result does not depend on set
    iteration order (not that a matching size could).
    """
    matched_gold: dict[str, str] = {}

    def augment(a: str, seen: set[str]) -> bool:
        for g in gold:
            if (a, g) not in ok or g in seen:
                continue
            seen.add(g)
            if g not in matched_gold or augment(matched_gold[g], seen):
                matched_gold[g] = a
                return True
        return False

    return sum(1 for a in assigned if augment(a, set()))


def _score_rule(
    rule: MatchRule,
    assigned: tuple[str, ...],
    gold: tuple[str, ...],
    relations: tuple[PairRelation, ...],
) -> RuleScore:
    if not assigned:
        # Coverage, not accuracy: no element counts, see the module docstring.
        return RuleScore(rule, Outcome.NO_ASSIGNMENT, None, None, None)
    ok = {(pair.assigned, pair.gold) for pair in relations if pair.satisfies(rule)}
    matched = _maximum_matching(assigned, gold, ok)
    counts = (matched, len(assigned) - matched, len(gold) - matched)
    grounded = {a for a, _ in ok}
    ungrounded = [a for a in assigned if a not in grounded]
    if not ungrounded:
        if matched < len(gold):
            return RuleScore(rule, Outcome.COLLAPSED, *counts)
        if len(assigned) > len(gold):
            return RuleScore(rule, Outcome.OVER_ASSIGNED, *counts)
        return RuleScore(rule, Outcome.CORRECT, *counts)
    stale = {
        pair.assigned for pair in relations if pair.relation is Relation.PREDECESSOR_OF_GOLD
    }
    # STALE needs an empty matching: when M == 0 nothing is grounded, so the
    # ungrounded ids are the whole assignment and all of them must be stale.
    if matched == 0 and all(a in stale for a in ungrounded):
        return RuleScore(rule, Outcome.STALE, *counts)
    if matched > 0:
        return RuleScore(rule, Outcome.PARTIAL, *counts)
    return RuleScore(rule, Outcome.WRONG, *counts)


def score_item(gold: GoldLabel, assigned: Iterable[str], graph: RorGraph) -> MatchResult:
    """Score one item under all three rules. The only evaluation path.

    Takes no source identifier, by design. See the module docstring for every
    definition this applies.
    """
    assigned_ids = normalise_ror_ids(assigned)
    if not gold.resolved:
        outcome = (
            Outcome.GOLD_AMBIGUOUS if gold.decision is Decision.AMBIGUOUS else Outcome.GOLD_NO_ROR
        )
        return MatchResult(
            gold=gold,
            assigned=assigned_ids,
            relations=(),
            by_rule={rule: RuleScore(rule, outcome, None, None, None) for rule in MatchRule},
        )

    assigned_sorted = tuple(sorted(assigned_ids))
    gold_sorted = tuple(sorted(gold.ror_ids))
    for gold_id in gold_sorted:
        if gold_id in graph and graph.status(gold_id) != "active":
            # The gold standard should only ever name live organisations;
            # anything else is worth seeing in the run log before it shapes a
            # SUCCESSOR_OF_GOLD relation.
            log.warning("gold_ror_id_not_active", ror_id=gold_id, status=graph.status(gold_id))
    relations = tuple(
        PairRelation(a, g, relation_between(a, g, graph))
        for a in assigned_sorted
        for g in gold_sorted
    )
    for pair in relations:
        if pair.relation is Relation.NOT_IN_DUMP:
            log.warning("assigned_ror_id_not_in_dump", ror_id=pair.assigned)
    return MatchResult(
        gold=gold,
        assigned=assigned_ids,
        relations=relations,
        by_rule={
            rule: _score_rule(rule, assigned_sorted, gold_sorted, relations) for rule in MatchRule
        },
    )


class AncestryIndex:
    """Shortest monotone path over parent/child edges: the sensitivity diagnostic.

    distance(a, b) is the number of parent edges climbed from one id to reach
    the other, in whichever direction is shorter, or None when neither is an
    ancestor of the other within max_depth. Identity is 0 and a direct
    parent/child edge is 1, so the PARENT_CHILD rule is exactly distance <= 1.

    Only monotone paths count. Climbing to a shared parent and back down (two
    institutes of one academy, or a hospital reaching a university through a
    joint research centre they both list as a child) is not ancestry, and
    admitting it is the degeneracy the module docstring describes. Edges are
    taken from whichever end asserts them, which needs a reverse index over
    the child lists, built once here.
    """

    def __init__(self, graph: RorGraph) -> None:
        parents: dict[str, set[str]] = {}
        for ror_id, sets in graph.relationship_index.items():
            if sets.parent:
                parents.setdefault(ror_id, set()).update(sets.parent)
            for child in sets.child:
                parents.setdefault(child, set()).add(ror_id)
        self._parents: dict[str, frozenset[str]] = {
            ror_id: frozenset(ids) for ror_id, ids in parents.items()
        }

    def parents_of(self, ror_id: str) -> frozenset[str]:
        return self._parents.get(ror_id, frozenset())

    def _climb(self, start: str, target: str, max_depth: int) -> int | None:
        if start == target:
            return 0
        seen = {start}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for parent in self.parents_of(node):
                if parent == target:
                    return depth + 1
                if parent not in seen:
                    seen.add(parent)
                    queue.append((parent, depth + 1))
        return None

    def distance(self, a: str, b: str, max_depth: int = DEFAULT_ANCESTRY_MAX_DEPTH) -> int | None:
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        a = normalise_ror_id(a)
        b = normalise_ror_id(b)
        climbs = (self._climb(a, b, max_depth), self._climb(b, a, max_depth))
        candidates = [d for d in climbs if d is not None]
        return min(candidates) if candidates else None

    def min_distance(
        self,
        assigned: Iterable[str],
        gold: Iterable[str],
        max_depth: int = DEFAULT_ANCESTRY_MAX_DEPTH,
    ) -> int | None:
        """The closest any assigned id comes to any gold id. None for an empty side."""
        distances = [
            d
            for a in assigned
            for g in gold
            if (d := self.distance(a, g, max_depth)) is not None
        ]
        return min(distances) if distances else None
