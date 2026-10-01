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
  ANY_RELATIONSHIP  exact, or the two ids are joined by a single direct
                    parent, child or related edge in either direction.

Under every rule the assigned id must also be active in the pinned release:
an id that is inactive or withdrawn there, or absent from it, grounds nothing
whatever edge joins it to the gold. Succession edges (predecessor, successor)
make a hit under no rule. They are still read, along the whole chain, but
only to classify an assignment as STALE rather than WRONG; see "Successor
chains" and "Stale ids" below.

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
That order never decides whether a dead id grounds (it never does, see "Stale
ids"), so a dead id joined to the gold by both a hierarchy edge and a
successor chain is reported under the hierarchy edge and is still stale; the
ids an item was stale on are carried separately in
:attr:`MatchResult.stale_assigned` so the taxonomy stays exact.

Stale ids. An assigned id that is not active in the pinned release never
grounds under any rule: whatever edge joins it to the gold, it is a false
positive, because the source asserted an identifier the registry no longer
holds as live. STALE is the outcome when every assigned id is such a dead id
and lies on a successor chain, of any length, to some gold id: the whole
assignment is the right institution under superseded identifiers (a renamed
or merged institution, however many times). It is reported under every rule,
never a plain miss and never a hit, because it is one of the error-taxonomy
categories in the brief and the reader is entitled to see it under whichever
rule they prefer. A dead id with no chain to any gold id is simply wrong. The
stale rate is published per source, and "stale credited as correct" is
reported as a one-line sensitivity so a reader who disagrees with the policy
can see what it costs each source.

Gold labels name the active record. :func:`score_item` refuses a gold id
whose status in the pinned release is not "active", naming the id, its status
and its successor(s). That is a gold-standard integrity error, not a source
error, and the review app enforces the same thing at labelling time. It does
not follow that a well-formed gold standard produces PREDECESSOR_OF_GOLD
only: v2.13 holds 16 active records with a successor edge to another active
record (De La Salle University 04xftk194 to De La Salle Medical and Health
Sciences Institute 012mgrb02; Academisch Ziekenhuis Rotterdam 00xtwy257 to
Erasmus MC 018906e22), so either succession relation can arise between two
live ids. Nothing is dead there, so neither is stale: the assignment is
WRONG unless a hierarchy edge or an exact match grounds it, in which case
that is the relation reported.

Set-level scoring. Gold labels can be sets (multi-affiliation items) and so can
assignments. Let G be the gold set, A the assigned set, and M the size of a
maximum bipartite matching between A and G where a pair may be matched if the
assigned id is active and the pair satisfies the rule. An assigned id is
"grounded" if it is active in the pinned release and satisfies the rule with
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
                 university). Impossible under EXACT. The extra ids are false
                 positives under every rule; see gate decision 3.
  STALE          no assigned id is grounded (M == 0) and every assigned id is
                 non-active in the pinned release and a predecessor, along a
                 chain of any length, of some gold id. The whole assignment
                 is the right institution under dead identifiers and nothing
                 else. Because a dead id grounds nothing under any rule, an
                 assignment that is STALE under one rule is STALE under all
                 three, with the same counts, whether or not the dead id also
                 carries a hierarchy or related edge to the gold.
  PARTIAL        at least one assigned id is ungrounded and M > 0: some of the
                 assignment is right and some of it is wrong. This includes an
                 assignment that mixes correct ids with a stale duplicate or a
                 stale sibling; the stale id is still recorded in
                 stale_assigned, so it stays countable in the error taxonomy
                 without the item being labelled "stale id" when half of it
                 was right.
  WRONG          at least one assigned id is ungrounded and M == 0. This is
                 where a dead id with no chain to any gold id lands, and
                 where an active id whose only link to the gold is a
                 succession edge lands.

Element counts. Every outcome in CORRECT, COLLAPSED, OVER_ASSIGNED, STALE,
PARTIAL and WRONG carries true_positives = M, false_positives = |A| - M and
false_negatives = |G| - M. NO_ASSIGNMENT and GOLD_AMBIGUOUS carry None for all
three. GOLD_NO_ROR carries None when nothing was assigned and (0, |A|, 0) when
something was; see the next paragraph. Accuracy metrics (precision, recall,
F1) are therefore micro-averaged by :func:`sum_element_counts` over exactly
the scores that carry counts, and it refuses to mix rules. Coverage, the share
of scorable items on which a source assigned anything at all
(:attr:`MatchResult.has_assignment`), is the complementary published figure
and is always reported next to accuracy. Folding abstentions into recall by
giving them false negatives would let two analysts publish two different
recall figures for the same source; the None values make that impossible to
do by accident.

Ambiguous and no_ror gold labels are first-class labels, not failures to label.
An item whose gold decision is ambiguous scores GOLD_AMBIGUOUS under every
rule, whatever the source assigned, with no element counts: the evidence does
not say what the right answer was, so nothing the source did can be judged.
An item whose gold decision is no_ror scores GOLD_NO_ROR under every rule, so
the rate is reportable, and the counts depend on what the source did. An
empty assignment carries None: abstaining where no ROR record exists is the
right answer, and it is not a coverage failure either, which is why the
outcome is GOLD_NO_ROR and not NO_ASSIGNMENT. A non-empty assignment is a
precision failure and carries true_positives 0, false_positives equal to the
number of distinct normalised assigned ids, and false_negatives 0 (there is no
gold set to miss). Neither GOLD_AMBIGUOUS nor GOLD_NO_ROR is scorable: neither
belongs in a coverage denominator, because an abstention on either is not a
failure to cover.

Gold ids outside the release. :func:`score_item` raises ValueError when a gold
id is absent from the pinned release, as does :func:`relation_between`, and
this module keeps doing so on purpose. The remedy belongs at labelling time:
the review app refuses ids outside the pinned release, so a frozen gold
standard can never name an organisation the scorer cannot see. An error here
therefore means that guard was bypassed, which is a gold-standard integrity
problem, not a source error, and silently scoring around it would launder a
broken gold standard into a published number. A gold id that is in the
release but not active is refused for the same reason (see "Gold labels name
the active record" above).

Gate decisions, settled 2026-10-01. Each was a project owner decision, applied
to every source equally, and each is pinned by a golden case:

  1. Succession edges never make a hit under any rule, so ANY_RELATIONSHIP
     grounds an assignment only through exact, parent, child or related
     edges and STALE is visible under all three rules. Reasoning: a stale id
     is a real, countable error in the brief's taxonomy, and a rule that
     hides it would reward a source for never refreshing its registry copy.
     Tightened 2026-10-01 after a verifier showed that the relation
     priority order was deciding whether a dead id grounds (19 real dead
     records with a hierarchy edge beside their chain grounded under the
     hierarchy rules, 6 with a related edge did not): an assigned id that is
     not active in the pinned release now grounds under no rule, so STALE
     means exactly a dead id on a chain to the gold, and a live predecessor
     of the gold is wrong rather than stale. Gold labels must name the active
     record, so a non-active gold id is an integrity error and the scorer
     raises rather than warns.
  2. An assignment made against a no_ror gold item is a precision failure,
     counted as false positives under GOLD_NO_ROR, while an empty assignment
     there carries no counts. Reasoning: a source that invents an affiliation
     where the annotator found no ROR record has asserted something wrong,
     and not charging for it would let aggressive assignment come at no
     cost; abstaining there is the right answer and must not be reported as
     a coverage failure.
  3. OVER_ASSIGNED stays a false positive under every rule, with its rate
     published. Reasoning: the extra ids are assertions the author did not
     make, and hierarchy tolerance is about which level of one institution
     was chosen, not about asserting more institutions.
  4. The ancestry-distance diagnostic keeps :data:`DEFAULT_ANCESTRY_MAX_DEPTH`
     and the distribution to that depth is what gets reported. Reasoning: the
     diagnostic is a sensitivity analysis rather than a rule, so one fixed
     reporting depth shared by every source is all it needs, and stating the
     depth here lets a reader rerun the diagnostic at another.

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
    # A successor chain leads to the gold id. Stale when the assigned id is
    # dead; a live predecessor of the gold (16 active records in v2.13 carry a
    # successor edge to another active record) is wrong, not stale.
    PREDECESSOR_OF_GOLD = "predecessor_of_gold"
    # A successor chain leads from the gold id. A dead gold id is refused by
    # score_item, so this arises between two live records, through the same
    # 16 active-with-successor records, and the outcome is wrong unless a
    # hierarchy edge also exists (which would then be the relation reported).
    SUCCESSOR_OF_GOLD = "successor_of_gold"
    RELATED = "related"
    NONE = "none"  # both ids are in the release, no direct edge joins them
    NOT_IN_DUMP = "not_in_dump"  # the assigned id is absent from the pinned release


# Which relations satisfy which rule. Succession relations are in none of them
# (gate decision 1). Satisfying a rule is necessary for grounding, not
# sufficient: _score_rule also requires the assigned id to be active in the
# pinned release, which is what keeps STALE visible under all three rules. See
# the module docstring.
_RULE_RELATIONS: Mapping[MatchRule, frozenset[Relation]] = {
    MatchRule.EXACT: frozenset({Relation.EXACT}),
    MatchRule.PARENT_CHILD: frozenset(
        {Relation.EXACT, Relation.PARENT_OF_GOLD, Relation.CHILD_OF_GOLD}
    ),
    MatchRule.ANY_RELATIONSHIP: frozenset(
        {Relation.EXACT, Relation.PARENT_OF_GOLD, Relation.CHILD_OF_GOLD, Relation.RELATED}
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


# The gold side is not resolved: these outcomes are never hits and never belong
# in a coverage denominator. GOLD_NO_ROR can still carry false positives.
UNSCORED_OUTCOMES = frozenset({Outcome.GOLD_AMBIGUOUS, Outcome.GOLD_NO_ROR})
# Outcomes that never carry element counts. NO_ASSIGNMENT is scorable (the gold
# is resolved) but is coverage, not accuracy; see the module docstring.
UNCOUNTED_OUTCOMES = frozenset({Outcome.GOLD_AMBIGUOUS, Outcome.NO_ASSIGNMENT})
# Outcomes that always carry element counts. GOLD_NO_ROR is in neither set: it
# carries counts exactly when something was assigned (gate decision 2).
COUNTED_OUTCOMES = frozenset(set(Outcome) - UNCOUNTED_OUTCOMES - {Outcome.GOLD_NO_ROR})


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
        """Whether the relation alone is a qualifying one under the rule.

        Grounding also needs the assigned id to be active in the pinned
        release; :func:`_score_rule` applies that on top of this.
        """
        return self.relation in _RULE_RELATIONS[rule]


@dataclass(frozen=True)
class RuleScore:
    """One item under one rule.

    Counts are all None or all present. They are always present for an
    outcome in COUNTED_OUTCOMES, never present for one in UNCOUNTED_OUTCOMES
    (the gold is ambiguous, or nothing was assigned), and for GOLD_NO_ROR
    present exactly when something was assigned, in which case they can only
    be false positives. The constructor enforces all of that, so a count can
    never be summed from a score that must not have one.
    """

    rule: MatchRule
    outcome: Outcome
    true_positives: int | None
    false_positives: int | None
    false_negatives: int | None

    def __post_init__(self) -> None:
        counts = (self.true_positives, self.false_positives, self.false_negatives)
        present = [count is not None for count in counts]
        if self.outcome in COUNTED_OUTCOMES and not all(present):
            raise ValueError(f"a {self.outcome.value} score must carry element counts")
        if self.outcome in UNCOUNTED_OUTCOMES and any(present):
            raise ValueError(f"a {self.outcome.value} score must not carry element counts")
        if any(present) and not all(present):
            raise ValueError(f"a {self.outcome.value} score must carry all three counts or none")
        # Gate decision 2: an assignment against a no_ror gold is only ever
        # false positives, and only when something was in fact assigned.
        if (
            self.outcome is Outcome.GOLD_NO_ROR
            and all(present)
            and (self.true_positives != 0 or self.false_negatives != 0 or not self.false_positives)
        ):
            raise ValueError(
                f"a gold_no_ror score with counts carries only false positives, got {counts}"
            )

    @property
    def scorable(self) -> bool:
        """The gold side is resolved, so the item belongs in a coverage denominator."""
        return self.outcome not in UNSCORED_OUTCOMES

    @property
    def counted(self) -> bool:
        """The item carries element counts, so it belongs in an accuracy denominator."""
        return self.true_positives is not None

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
    # How many scores contributed. Abstentions and ambiguous gold are not among
    # them; a no_ror gold item with an assignment is (as false positives only).
    items: int


def sum_element_counts(rule: MatchRule, scores: Iterable[RuleScore]) -> ElementCounts:
    """Sum element counts over the counted items under one rule, for micro-averaging.

    NO_ASSIGNMENT and GOLD_AMBIGUOUS scores are skipped, as is GOLD_NO_ROR
    with an empty assignment, so an abstention never reaches a recall
    denominator here; coverage is reported from
    :attr:`MatchResult.has_assignment` instead. GOLD_NO_ROR with an assignment
    contributes its false positives, so precision pays for an invented
    affiliation while recall is untouched (gate decision 2). A score under a
    different rule is an error, not something to average over.
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
    # The assigned ids that are non-active in the pinned release and lie on a
    # successor chain to some gold id. Rule-independent, like the relations,
    # and the item is STALE exactly when this is the whole non-empty
    # assignment. Carried separately because the relation reported for such
    # an id can be a hierarchy edge (see the priority order in the module
    # docstring), so the relations alone would undercount stale ids.
    stale_assigned: frozenset[str] = frozenset()

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


def _active_assigned_ids(assigned: Iterable[str], graph: RorGraph) -> frozenset[str]:
    """The assigned ids the pinned release holds with status "active".

    An id the release does not hold at all is not active in it either, so it
    is left out here and grounds nothing, the same as an inactive or
    withdrawn one.
    """
    return frozenset(a for a in assigned if a in graph and graph.status(a) == "active")


def _stale_assigned_ids(
    assigned: Iterable[str], gold: Iterable[str], active: frozenset[str], graph: RorGraph
) -> frozenset[str]:
    """The non-active assigned ids with a successor chain, of any length, to some gold id.

    Read from the succession index directly rather than from the recorded
    relations, because a dead id with a hierarchy edge beside its chain is
    reported under the hierarchy edge (see the priority order in the module
    docstring) and is stale all the same.
    """
    succession = succession_index_for(graph)
    gold_ids = tuple(gold)
    return frozenset(
        a
        for a in assigned
        if a not in active and any(succession.supersedes(a, g) for g in gold_ids)
    )


def _score_rule(
    rule: MatchRule,
    assigned: tuple[str, ...],
    gold: tuple[str, ...],
    relations: tuple[PairRelation, ...],
    active: frozenset[str],
    stale: frozenset[str],
) -> RuleScore:
    if not assigned:
        # Coverage, not accuracy: no element counts, see the module docstring.
        return RuleScore(rule, Outcome.NO_ASSIGNMENT, None, None, None)
    # Grounding needs a live id and a qualifying relation. A non-active
    # assigned id is a false positive whatever edge joins it to the gold.
    ok = {
        (pair.assigned, pair.gold)
        for pair in relations
        if pair.assigned in active and pair.satisfies(rule)
    }
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
    # STALE needs an empty matching: when M == 0 nothing is grounded, so the
    # ungrounded ids are the whole assignment and all of them must be stale.
    # Stale ids are dead and so never ground, which is why this does not
    # depend on the rule.
    if matched == 0 and all(a in stale for a in ungrounded):
        return RuleScore(rule, Outcome.STALE, *counts)
    if matched > 0:
        return RuleScore(rule, Outcome.PARTIAL, *counts)
    return RuleScore(rule, Outcome.WRONG, *counts)


def _score_unresolved_gold(rule: MatchRule, gold: GoldLabel, assigned: frozenset[str]) -> RuleScore:
    """GOLD_AMBIGUOUS or GOLD_NO_ROR, with the counts gate decision 2 gives them.

    Ambiguous gold carries no counts whatever was assigned. No_ror gold
    carries none for an empty assignment (abstaining is the right answer) and
    one false positive per distinct normalised assigned id otherwise.
    """
    if gold.decision is Decision.AMBIGUOUS:
        return RuleScore(rule, Outcome.GOLD_AMBIGUOUS, None, None, None)
    if not assigned:
        return RuleScore(rule, Outcome.GOLD_NO_ROR, None, None, None)
    return RuleScore(rule, Outcome.GOLD_NO_ROR, 0, len(assigned), 0)


def _require_active_gold_id(gold_id: str, graph: RorGraph) -> None:
    """Refuse a gold id the pinned release does not know or does not hold as active.

    Either is a gold-standard integrity error, not a source error, and the
    review app is meant to have refused it at labelling time; see the module
    docstring. The successors are named so the owner can see which active
    record the label should have carried.
    """
    if gold_id not in graph:
        raise ValueError(f"gold ROR id {gold_id} is not in the pinned ROR release")
    status = graph.status(gold_id)
    if status == "active":
        return
    successors = sorted(succession_index_for(graph).successors_of(gold_id))
    superseded_by = ", ".join(successors) if successors else "no recorded successor"
    log.error("gold_ror_id_not_active", ror_id=gold_id, status=status, successors=successors)
    raise ValueError(
        f"gold ROR id {gold_id} has status {status} in the pinned ROR release "
        f"(successor: {superseded_by}); gold labels must name the active record"
    )


def score_item(gold: GoldLabel, assigned: Iterable[str], graph: RorGraph) -> MatchResult:
    """Score one item under all three rules. The only evaluation path.

    Takes no source identifier, by design. See the module docstring for every
    definition this applies. Raises ValueError for a gold id that is outside
    the pinned release or not active in it.
    """
    assigned_ids = normalise_ror_ids(assigned)
    if not gold.resolved:
        return MatchResult(
            gold=gold,
            assigned=assigned_ids,
            relations=(),
            by_rule={rule: _score_unresolved_gold(rule, gold, assigned_ids) for rule in MatchRule},
        )

    assigned_sorted = tuple(sorted(assigned_ids))
    gold_sorted = tuple(sorted(gold.ror_ids))
    for gold_id in gold_sorted:
        _require_active_gold_id(gold_id, graph)
    relations = tuple(
        PairRelation(a, g, relation_between(a, g, graph))
        for a in assigned_sorted
        for g in gold_sorted
    )
    for pair in relations:
        if pair.relation is Relation.NOT_IN_DUMP:
            log.warning("assigned_ror_id_not_in_dump", ror_id=pair.assigned)
    active = _active_assigned_ids(assigned_sorted, graph)
    stale = _stale_assigned_ids(assigned_sorted, gold_sorted, active, graph)
    return MatchResult(
        gold=gold,
        assigned=assigned_ids,
        relations=relations,
        by_rule={
            rule: _score_rule(rule, assigned_sorted, gold_sorted, relations, active, stale)
            for rule in MatchRule
        },
        stale_assigned=stale,
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
