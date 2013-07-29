import z3
from third_party import enum
from core.callhistory import CallHistory
from core.call import Call
from core.hand import Hand
from core.callexplorer import CallExplorer
import core.suit as suit
from itertools import chain
import copy
from third_party.memoized import memoized



spades, hearts, diamonds, clubs, points = z3.Ints('spades hearts diamonds clubs points')

axioms = [
    spades + hearts + diamonds + clubs == 13,
    spades >= 0,
    hearts >= 0,
    diamonds >= 0,
    clubs >= 0,
    0 <= points <= 37,
]

rule_of_twenty = z3.Or(
    spades + hearts + points >= 20,
    spades + diamonds + points >= 20,
    spades + clubs + points >= 20,
    hearts + diamonds + points >= 20,
    hearts + clubs + points >= 20,
    diamonds + clubs + points >= 20)

rule_of_nineteen = z3.Or(
    spades + hearts + points >= 19,
    spades + diamonds + points >= 19,
    spades + clubs + points >= 19,
    hearts + diamonds + points >= 19,
    hearts + clubs + points >= 19,
    diamonds + clubs + points >= 19)

rule_of_fifteen = spades + points >= 15

balanced = z3.And(clubs >= 2, diamonds >= 2, hearts >= 2, spades >= 2,
    z3.Or(
        z3.And(hearts > 2, diamonds > 2, clubs > 2),
        z3.And(spades > 2, diamonds > 2, clubs > 2),
        z3.And(spades > 2, hearts > 2, clubs > 2),
        z3.And(spades > 2, hearts > 2, diamonds > 2),
    )
)

NO_CONSTRAINTS = z3.BoolVal(True)


def expr_for_suit(suit):
    return (clubs, diamonds, hearts, spades)[suit]


def expr_for_hand(hand):
    return z3.And(
        clubs == len(hand.cards_in_suit(suit.CLUBS)),
        diamonds == len(hand.cards_in_suit(suit.DIAMONDS)),
        hearts == len(hand.cards_in_suit(suit.HEARTS)),
        spades == len(hand.cards_in_suit(suit.SPADES)),
        points == hand.high_card_points()
    )



class SolverPool(object):
    @memoized
    def solver_for_hand(self, hand):
        solver = z3.Solver()
        solver.add(axioms)
        solver.add(expr_for_hand(hand))
        return solver


solver_pool = SolverPool()


# Intra-bid priorities, first phase, "interpretation priorities", like "natural, conventional" (possibly should be called types?) These select which "1N" meaning is correct.
# Inter-bid priorities, "which do you look at first" -- these order preference between "1H, vs. 1S"
# Tie-breaker-priorities -- planner stage, when 2 bids match which we make.

positions = enum.Enum(
    "RHO",
    "Partner",
    "LHO",
    "Me",
)


annotations = enum.Enum(
    "Opening",
    "NoTrumpSystemsOn",
    "Artificial",
    "Stayman",
    "Gerber",
)


categories = enum.Enum(
    "Relay",
    "FeatureAsking",
    "NoTrump",
)


class Precondition(object):
    def __repr__(self):
        return "%s()" % self.name()

    def name(self):
        return self.__class__.__name__

    def fits(self, history, call):
        pass


class InvertedPrecondition(Precondition):
    def __init__(self, precondition):
        self.precondition = precondition

    def name(self):
        return "not" + self.precondition.name()

    def fits(self, history, call):
        return not self.precondition.fits(history, call)


class NoOpening(Precondition):
    def fits(self, history, call):
        return annotations.Opening not in history.annotations


class Opened(Precondition):
    def __init__(self, position):
        self.position = position

    def fits(self, history, call):
        return annotations.Opening in history.annotations_for_position(self.position)


class LastBidHasAnnotation(Precondition):
    def __init__(self, position, annotation):
        self.position = position
        self.annotation = annotation

    def fits(self, history, call):
        return self.annotation in history.view_for(self.position).annotations_for_last_call


class LastBidHasStrain(Precondition):
    def __init__(self, position, strain):
        self.position = position
        self.strain = strain

    def fits(self, history, call):
        last_call = history.view_for(self.position).last_call
        return last_call and last_call.strain == self.strain


class RaiseOfPartnersLastSuit(Precondition):
    def fits(self, history, call):
        partner_last_call = history.partner.last_call
        if not partner_last_call or partner_last_call.strain not in suit.SUITS:
            return False
        return call.strain == partner_last_call.strain and history.partner.min_length(partner_last_call.strain) >= 3


class UnbidSuit(Precondition):
    def fits(self, history, call):
        if call.strain not in suit.SUITS:
            return False
        return history.is_unbid_suit(call.strain)


class Jump(Precondition):
    def __init__(self, exact_size=None):
        self.exact_size = exact_size

    def _jump_size(self, last_call, call):
        if call.strain <= last_call.strain:
            # If the new suit is less than the last bid one, than we need to change more than one level for it to be a jump.
            return call.level() - last_call.level() - 1
        # Otherwise any bid not at the current level is a jump.
        return call.level() - last_call.level()

    def fits(self, history, call):
        if call.is_pass():
            return False
        if call.is_double() or call.is_redouble():
            call = history.call_history.last_contract()

        last_call = self._last_call(history)
        if not last_call or not last_call.is_contract():  # If we don't have a previous bid to compare to, this can't be a jump.
            return False
        jump_size = self._jump_size(last_call, call)
        if self.exact_size is None:
            return jump_size != 0
        return self.exact_size == jump_size

    def _last_call(self, history):
        raise NotImplementedError


class JumpFromLastContract(Jump):
    def _last_call(self, history):
        return history.call_history.last_contract()


class JumpFromMyLastBid(Jump):
    def _last_call(self, history):
        return history.me.last_call


class JumpFromPartnerLastBid(Jump):
    def _last_call(self, history):
        return history.partner.last_call


class NotJumpFromLastContract(JumpFromLastContract):
    def __init__(self):
        Jump.__init__(self, exact_size=0)


class NotJumpFromMyLastBid(JumpFromMyLastBid):
    def __init__(self):
        JumpFromMyLastBid.__init__(self, exact_size=0)


class NotJumpFromPartnerLastBid(JumpFromPartnerLastBid):
    def __init__(self):
        JumpFromPartnerLastBid.__init__(self, exact_size=0)


class Rule(object):
    # FIXME: Consider splitting call_preconditions out from preconditions
    # for preconditions which only operate on the call?
    preconditions = []
    category = None # Intra-bid priority
    requires_planning = False

    call_name = None # call_name = '1C' -> preconditons = [CallName('1C')]
    call_names = None # call_names = ['1C', '1D'] -> preconditons = [CallNames('1C', '1D')]

    constraints = {}
    shared_constraints = []
    annotations = []
    conditional_priorities = []
    priority = None

    def __init__(self):
        assert self.priority or self.constraints, "" + self.name() + " is missing priority"
        assert not self.conditional_priorities or not self.constraints
        # conditional_priorities only works with a single call_name.
        assert not self.conditional_priorities or self.call_name

    def name(self):
        return self.__class__.__name__

    def __repr__(self):
        return "%s()" % self.name()

    def fits_preconditions(self, history, call):
        for precondition in self.preconditions:
            if not precondition.fits(history, call):
                return False
        return True

    def _possible_calls_over(self, history):
        # If this Rule has explicit call restrictions, we only need to consider those.
        # FIXME: We should probably standardize this on some sort of call_preconditions instead.        
        if self.call_name:
            return [Call.from_string(self.call_name)]
        elif self.call_names:
            return map(Call.from_string, self.call_names)
        elif self.constraints:
            return map(Call.from_string, self.constraints.keys())
        # Otherwise we need to run all possible calls through the preconditions.
        return CallExplorer().possible_calls_over(history.call_history)

    def calls_over(self, history):
        for call in self._possible_calls_over(history):
            if self.fits_preconditions(history, call):
                yield call

    def possible_priorities_and_conditions_for_call(self, call):
        # conditional_priorities only work for a single call_name
        for condition, priority in self.conditional_priorities:
            yield priority, condition

        _, priority = self._per_call_constraints_and_priority(call)
        assert priority
        yield priority, NO_CONSTRAINTS

    @memoized
    def priority_for_call_and_hand(self, solver, history, call, hand):
        if not is_possible(solver, self.constraints_expr_for_call(history, call)):
            return None

        for condition, priority in self.conditional_priorities:
            if is_possible(solver, condition):
                return priority

        _, priority = self._per_call_constraints_and_priority(call)
        if priority:
            return priority

        return self.priority

    def _exprs_from_constraints(self, constraints, history, call):
        if not constraints:
            return [NO_CONSTRAINTS]

        if isinstance(constraints, Constraint):
            return [constraints.expr(history, call)]

        if isinstance(constraints, z3.ExprRef):
            return [constraints]

        return chain.from_iterable([self._exprs_from_constraints(constraint, history, call) for constraint in constraints])

    # constraints accepts various forms including:
    # constraints = { '1H': hearts > 5 }
    # constraints = { '1H': (hearts > 5, priority) }

    # FIXME: Should we split this into two methods? on for priority and one for constraints?
    def _per_call_constraints_and_priority(self, call):
        constraints_tuple = self.constraints.get(call.name)
        if not constraints_tuple:
            return None, self.priority

        try:
            if isinstance(list(constraints_tuple)[-1], enum.EnumValue):
                assert len(constraints_tuple) == 2
                return constraints_tuple
        except TypeError:
            return constraints_tuple, self.priority

    def constraints_expr_for_call(self, history, call):
        exprs = []
        per_call_constraints, _ = self._per_call_constraints_and_priority(call)
        if per_call_constraints:
            exprs.extend(self._exprs_from_constraints(per_call_constraints, history, call))
        exprs.extend(self._exprs_from_constraints(self.shared_constraints, history, call))
        return z3.And(exprs)


opening_priorities = enum.Enum(
    "StrongTwoClubs",
    "NoTrumpOpening",
    "LongestMajor",
    "HigherMajor",
    "LowerMajor",
    "LongestMinor",
    "HigherMinor",
    "LowerMinor",
)


class Opening(Rule):
    annotations = [annotations.Opening]
    preconditions = [NoOpening()]


class OneClubOpening(Opening):
    call_name = '1C'
    shared_constraints = [rule_of_twenty, clubs >= 3]
    conditional_priorities = [
        (z3.Or(clubs > diamonds, z3.And(clubs == 3, diamonds == 3)), opening_priorities.LongestMinor),
    ]
    priority = opening_priorities.LowerMinor


class OneDiamondOpening(Opening):
    call_name = '1D'
    shared_constraints = [rule_of_twenty, diamonds >= 3]
    conditional_priorities = [
        (diamonds > clubs, opening_priorities.LongestMinor),
    ]
    priority = opening_priorities.HigherMinor


class OneHeartOpening(Opening):
    call_name = '1H'
    shared_constraints = [rule_of_twenty, hearts >= 5]
    conditional_priorities = [
        (hearts > spades, opening_priorities.LongestMajor),
    ]
    priority = opening_priorities.LowerMajor


class OneSpadeOpening(Opening):
    call_name = '1S'
    shared_constraints = [rule_of_twenty, spades >= 5]
    conditional_priorities = [
        (spades > hearts, opening_priorities.LongestMajor),
    ]
    priority = opening_priorities.HigherMajor


class NoTrumpOpening(Opening):
    annotations = Opening.annotations + [annotations.NoTrumpSystemsOn]
    constraints = {
        '1N': z3.And(points >= 15, points <= 17, balanced),
        '2N': z3.And(points >= 20, points <= 21, balanced)
    }
    priority = opening_priorities.NoTrumpOpening


# class OneNoTrumpOpening(Opening):
#     call_name = '1N'
#     shared_constraints = 


# class TwoNoTrumpOpening(Opening):
#     annotations = Opening.annotations + [annotations.NoTrumpSystemsOn]
#     call_name = '2N'
#     shared_constraints = [points >= 20, points <= 21, balanced]
#     priority = opening_priorities.NoTrumpOpening


class StrongTwoClubs(Opening):
    call_name = '2C'
    shared_constraints = points >= 22  # FIXME: Should support "or 9+ winners"
    priority = opening_priorities.StrongTwoClubs


response_priorities = enum.Enum(
    "MajorLimitRaise",
    "MajorMinimumRaise",
    "LongestNewMajor",
    "OneSpadeWithFiveResponse",
    "OneHeartWithFiveResponse",
    "OneDiamondResponse",
    "OneHeartWithFourResponse",
    "OneSpadeWithFourResponse",
    "TwoHeartNewSuitResponse",
    "TwoSpadeNewSuitResponse",
    "TwoClubNewSuitResponse",
    "TwoDiamondNewSuitResponse",
    "OneNotrumpResponse",
)


class Response(Rule):
    preconditions = [Opened(positions.Partner)]


class OneDiamondResponse(Response):
    call_name = '1D'
    shared_constraints = [points >= 6, diamonds >= 4]
    priority = response_priorities.OneDiamondResponse


class OneHeartResponse(Response):
    call_name = '1H'
    shared_constraints = [points >= 6, hearts >= 4]
    conditional_priorities = [
        (z3.And(hearts >= 5, hearts > spades), response_priorities.LongestNewMajor),
        (hearts >= 5, response_priorities.OneHeartWithFiveResponse),
    ]
    priority = response_priorities.OneHeartWithFourResponse


class OneSpadeResponse(Response):
    call_name = '1S'
    shared_constraints = [points >= 6, spades >= 4]
    conditional_priorities = [
        (spades >= 5, response_priorities.OneSpadeWithFiveResponse)
    ]
    priority = response_priorities.OneSpadeWithFourResponse


class OneNotrumpResponse(Response):
    call_name = '1N'
    shared_constraints = points >= 6
    priority = response_priorities.OneNotrumpResponse



class Constraint(object):
    def expr(self, history, call):
        pass


class Z3(Constraint):
    def __init__(self, expr):
        self._expr = expr

    def expr(self, history, call):
        return self._expr


class MinimumCombinedLength(Constraint):
    def __init__(self, min_count):
        self.min_count = min_count

    def expr(self, history, call):
        suit = call.strain
        partner_promised_length = history.partner.min_length(suit)
        implied_length = max(self.min_count - partner_promised_length, 0)
        return expr_for_suit(suit) >= implied_length


class MinLength(Constraint):
    def __init__(self, min_length):
        self.min_length = min_length

    def expr(self, history, call):
        return expr_for_suit(call.strain) >= self.min_length


class RaiseResponse(Response):
    preconditions = Response.preconditions + [RaiseOfPartnersLastSuit(), LastBidHasAnnotation(positions.Partner, annotations.Opening)]


class MajorMinimumRaise(RaiseResponse):
    call_names = ['2H', '2S']
    shared_constraints = [MinimumCombinedLength(8), points >= 6]
    priority = response_priorities.MajorMinimumRaise


class MajorLimitRaise(RaiseResponse):
    call_names = ['3H', '3S']
    shared_constraints = [MinimumCombinedLength(8), points >= 10]
    priority = response_priorities.MajorLimitRaise


# We should bid longer suits when possible, up the line for 4 cards.
# we don't currently bid 2D over 2C when we have longer diamonds.

class NewSuitAtTheTwoLevel(Response):
    preconditions = Response.preconditions + [UnbidSuit(), NotJumpFromLastContract()]
    constraints = {
        '2C' : (clubs >= 4, response_priorities.TwoClubNewSuitResponse),
        '2D' : (diamonds >= 4, response_priorities.TwoDiamondNewSuitResponse),
        '2H' : (hearts >= 5, response_priorities.TwoHeartNewSuitResponse),
        '2S' : (spades >= 5, response_priorities.TwoSpadeNewSuitResponse),
    }
    shared_constraints = points >= 10


nt_response_priorities = enum.Enum(
    "NoTrumpJumpRaise",
    "NoTrumpMinimumRaise",
    "JacobyTransferToLongerMajor",
    "JacobyTransferToSpadesWithGameForcingValues",
    "JacobyTransferToHeartsWithGameForcingValues",
    "JacobyTransferToHearts",
    "JacobyTransferToSpades",
    "Stayman",
    "ClubBust",
)


class NoTrumpResponse(Response):
    category = categories.NoTrump
    preconditions = Response.preconditions + [
        LastBidHasAnnotation(positions.Partner, annotations.Opening),
        LastBidHasAnnotation(positions.Partner, annotations.NoTrumpSystemsOn),
    ]


class BasicStayman(NoTrumpResponse):
    call_name = '2C'
    annotations = Response.annotations + [annotations.Artificial, annotations.Stayman]
    priority = nt_response_priorities.Stayman
    shared_constraints = [points >= 8, z3.Or(hearts >= 4, spades >= 4)]


class TransferTo(object):
    def __init__(self, suit):
        self.suit = suit


class JacobyTransfer(NoTrumpResponse):
    annotations = NoTrumpResponse.annotations + [annotations.Artificial, TransferTo(suit.HEARTS)]


class JacobyTransferToHearts(JacobyTransfer):
    call_name = '2D'
    shared_constraints = hearts >= 5
    conditional_priorities = [
        (hearts > spades, nt_response_priorities.JacobyTransferToLongerMajor),
        (z3.And(hearts == spades, points >= 10), nt_response_priorities.JacobyTransferToHeartsWithGameForcingValues),
    ]
    priority = nt_response_priorities.JacobyTransferToHearts


class JacobyTransferToSpades(JacobyTransfer):
    call_name = '2H'
    shared_constraints = spades >= 5
    conditional_priorities = [
        (spades > hearts, nt_response_priorities.JacobyTransferToLongerMajor),
        (z3.And(hearts == spades, points >= 10), nt_response_priorities.JacobyTransferToSpadesWithGameForcingValues),
    ]
    priority = nt_response_priorities.JacobyTransferToSpades


# FIXME: We don't support multiple call names...
# class AcceptTransfer(Rule):
#     category = categories.Relay
#     preconditions = Rule.preconditions + [
#         LastBidHasAnnotationOfClass(positions.Partner, TransferTo),
#         NotJumpFromPartnerLastBid(),
#     ]


stayman_response_priorities = enum.Enum(
    "HeartStaymanResponse",
    "SpadeStaymanResponse",
    "DiamondStaymanResponse",
)


class StaymanResponse(Rule):
    preconditions = Rule.preconditions + [LastBidHasAnnotation(positions.Partner, annotations.Stayman)]
    constraints = {
        '2D': (NO_CONSTRAINTS, stayman_response_priorities.DiamondStaymanResponse),
        '2H': (Z3(hearts >= 4), stayman_response_priorities.HeartStaymanResponse),
        '2S': (Z3(spades >= 4), stayman_response_priorities.SpadeStaymanResponse)
    }


overcall_priorities = enum.Enum(
    # FIXME: This needs the prefer the longer suit pattern.
    "DirectOvercall",
)

class DirectOvercall(Rule):
    preconditions = Rule.preconditions + [LastBidHasAnnotation(positions.RHO, annotations.Opening)]
    priority = overcall_priorities.DirectOvercall


class OneLevelOvercall(DirectOvercall):
    call_names = ['1D', '1H', '1S']
    shared_constraints = [MinLength(5), points >= 8]


feature_asking_priorites = enum.Enum(
    "Gerber",
)


class Gerber(Rule):
    category = categories.FeatureAsking
    requires_planning = True
    shared_constraints = NO_CONSTRAINTS
    annotations = [annotations.Gerber]
    priority = feature_asking_priorites.Gerber


class GerberForAces(Gerber):
    call_name = '4C'
    preconditions = Gerber.preconditions + [
        LastBidHasStrain(positions.Partner, suit.NOTRUMP),
        InvertedPrecondition(LastBidHasAnnotation(positions.Partner, annotations.Artificial))
    ]


class GerberForKings(Gerber):
    call_name = '5C'
    preconditions = Gerber.preconditions + [
        LastBidHasAnnotation(positions.Me, annotations.Gerber)
    ]


class PartialOrdering(object):
    def __init__(self):
        self._values_greater_than = {}

    def make_less_than(self, lesser, greater):
        greater_values = self._values_greater_than.get(greater, set()).union(set([greater]))
        self._values_greater_than.setdefault(lesser, set()).update(greater_values)

    def less_than(self, left, right):
        # FIXME: enum.py should be asserting when comparing different types
        # but it seems to be silently succeeding in python 2.7.
        if left.enumtype != right.enumtype:
            return right.enumtype in self._values_greater_than.get(left.enumtype, set())
        # Our enums are written highest to lowest, so we use > for less_than. :)
        return left > right


# FIXME: This is wrong as soon as we try to support more than one system.
def _get_subclasses(base_class):
    subclasses = base_class.__subclasses__()
    for subclass in list(subclasses):
        subclasses.extend(_get_subclasses(subclass))
    return subclasses

def _concrete_rule_classes():
    return filter(lambda rule: not rule.__subclasses__(), _get_subclasses(Rule))


class StandardAmericanYellowCard(object):
    # Rule ordering does not matter.  We could have python crawl the files to generate this list instead.
    rules = [rule() for rule in _concrete_rule_classes()]
    priority_ordering = PartialOrdering()

    priority_ordering.make_less_than(response_priorities, nt_response_priorities)


# The dream:
# history.my.solver
# annotations.Opening in history.rho.annotations
# annotations.Opening in history.rho.last_call.annotations
# history.partner.min_length(suit)
# history.partner.max_length(suit)
# history.partner.min_hcp()
# history.partner.max_hcp()


class PositionView(object):
    def __init__(self, history, position):
        self.history = history
        self.position = position

    @property
    def annotations(self):
        return self.history.annotations_for_position(self.position)

    # FIXME: We could hang annotations off of the Call object, but currently
    # Call is from the old system.
    @property
    def annotations_for_last_call(self):
        return self.history.annotations_for_last_call(self.position)

    @property
    def last_call(self):
        return self.history.call_history.last_call_by(self.history._offset_from_dealer(self.position))

    def min_length(self, suit):
        return self.history.min_length_for_position(self.position, suit)


def is_valid(solver, expr):
    solver.push()
    solver.add(z3.Not(expr))
    result = solver.check() == z3.unsat
    solver.pop()
    return result


def is_possible(solver, expr):
    solver.push()
    solver.add(expr)
    result = solver.check() == z3.sat
    solver.pop()
    return result


# This class is immutable.
class History(object):
    def __init__(self, previous_history=None):
        self.call_history = CallHistory()
        self._annotation_history = []
        self._constraint_history = []
        self._previous_history = previous_history

    @property
    def annotations(self):
        return chain.from_iterable(self._annotation_history)

    def extend_with(self, call, annotations, constraints):
        history = History(self)
        history.call_history = copy.copy(self.call_history)
        history.call_history.calls.append(call)
        history._annotation_history = self._annotation_history + [annotations]
        history._constraint_history = self._constraint_history + [constraints]
        return history

    def _offset_from_dealer(self, position):
        return (len(self.call_history) - 1 - position.index) % 4

    def _project_for_position(self, items, position):
        end = -1 - position.index
        start = (len(items) + end) % 4
        return items[start::4]

    def _position_in_previous_history(self, position):
        return positions[(position.index - 1) % 4]


    @memoized
    def solver_for_position(self, position):
        if not self._previous_history:
            solver = z3.Solver()
            solver.add(axioms)
            return solver
        position_in_previous_history = self._position_in_previous_history(position)
        solver_in_previous_history = self._previous_history.solver_for_position.take(position_in_previous_history)
        if position_in_previous_history != positions.Me:
            return solver_in_previous_history
        solver = solver_in_previous_history
        solver.add(self._constraint_history[-1])
        return solver

    def annotations_for_position(self, position):
        return chain.from_iterable(self._project_for_position(self._annotation_history, position))

    def annotations_for_last_call(self, position):
        projection = self._project_for_position(self._annotation_history, position)
        if not projection:
            return []
        return projection[-1]

    @memoized
    def min_length_for_position(self, position, suit):
        solver = self.solver_for_position(position)
        suit_expr = expr_for_suit(suit)
        # FIXME: This would be faster as a binary search.
        for length in range(13, 0, -1):
            if is_valid(solver, suit_expr >= length):
                return length
        return 0

    @memoized
    def is_unbid_suit(self, suit):
        suit_expr = expr_for_suit(suit)
        for position in positions:
            solver = self.solver_for_position(position)
            if not is_possible(solver, suit_expr < 3):
                return False
        return True

    @property
    def rho(self):
        return PositionView(self, positions.RHO)

    @property
    def me(self):
        return PositionView(self, positions.Me)

    @property
    def partner(self):
        return PositionView(self, positions.Partner)

    @property
    def lho(self):
        return PositionView(self, positions.LHO)

    def view_for(self, position):
        return PositionView(self, position)


class PossibleCalls(object):
    def __init__(self, ordering):
        self.ordering = ordering
        self._calls_and_priorities = []

    def add_call_with_priority(self, call, priority):
        self._calls_and_priorities.append([call, priority])

    def _is_dominated(self, priority, maximal_calls_and_priorities):
        # First check to see if any existing call is larger than this one.
        for max_call, max_priority in maximal_calls_and_priorities:
            if self.ordering.less_than(priority, max_priority):
                return True
        return False

    def calls_of_maximal_priority(self):
        maximal_calls_and_priorities = []
        for call, priority in self._calls_and_priorities:
            if self._is_dominated(priority, maximal_calls_and_priorities):
                continue
            maximal_calls_and_priorities = filter(lambda (max_call, max_priority): not self.ordering.less_than(max_priority, priority), maximal_calls_and_priorities)
            maximal_calls_and_priorities.append([call, priority])
        return [call for call, _ in maximal_calls_and_priorities]


class Bidder(object):
    def __init__(self):
        # Assuming SAYC for all sides.
        self.system = StandardAmericanYellowCard

    def find_call_for(self, hand, call_history):
        history = Interpreter().create_history(call_history)
        # Select highest-intra-bid-priority (category) rules for all possible bids
        rule_selector = RuleSelector(self.system, history)
        # Compute inter-bid priorities (priority) for each using the hand.
        possible_calls = rule_selector.possible_calls_for_hand(history, hand)
        # The resulting priorities are only partially ordered, so have to be walked in a tree.
        maximal_calls = possible_calls.calls_of_maximal_priority()
        # Currently we have no tie-breaking priorities (no planner), so we just select the first call we found.
        maximal_calls = filter(lambda call: not rule_selector.rule_for_call(call).requires_planning, maximal_calls)
        if not maximal_calls:
            # If we failed to find any rule able to bid, this is an error.
            return None
        return maximal_calls[0]


class RuleSelector(object):
    def __init__(self, system, history):
        self.system = system
        self.history = history
        self._call_to_rule_cache = None
        self._call_to_compiled_constraints = {}

    def _call_to_rule_map(self):
        if self._call_to_rule_cache is not None:
            return self._call_to_rule_cache

        self._call_to_rule_cache = {}
        for rule in self.system.rules:
            for call in rule.calls_over(self.history):
                exisiting_rule = self._call_to_rule_cache.get(call)
                if not exisiting_rule or rule.category > exisiting_rule.category:
                    self._call_to_rule_cache[call] = rule
        return self._call_to_rule_cache

    def rule_for_call(self, call_to_lookup):
        return self._call_to_rule_map().get(call_to_lookup)

    def compile_constraints_for_call(self, history, call):
        constraints = self._call_to_compiled_constraints.get(call)
        if constraints:
            return constraints

        # (z3.Or(clubs > diamonds, clubs == diamonds == 3) AND !(ROT AND hearts >= 5) AND !(ROT AND spades >= 5))
        # OR
        # (!z3.Or(clubs > diamonds, clubs == diamonds == 3) AND !(ROT AND diamonds >=3) AND !(ROT AND hearts >= 5) AND !(ROT AND spades >= 5))

        situations = []
        used_rule = self.rule_for_call(call)
        for priority, condition in used_rule.possible_priorities_and_conditions_for_call(call):
            situational_constraints = [condition]
            for unmade_call, unmade_rule in self._call_to_rule_map().iteritems():
                for unmade_priority, unmade_condition in unmade_rule.possible_priorities_and_conditions_for_call(unmade_call):
                    if unmade_priority < priority: # FIXME: < means > for priority compares.
                        situational_constraints.append(z3.Not(z3.And(unmade_condition, unmade_rule.constraints_expr_for_call(history, unmade_call))))
            situations.append(z3.And(situational_constraints))
        constraints = z3.Or(situations)
        self._call_to_compiled_constraints[call] = constraints
        return constraints

    def possible_calls_for_hand(self, history, hand):
        possible_calls = PossibleCalls(self.system.priority_ordering)
        solver = solver_pool.solver_for_hand(hand)
        for call in CallExplorer().possible_calls_over(self.history.call_history):
            rule = self.rule_for_call(call)
            if not rule:
                continue
            priority = rule.priority_for_call_and_hand(solver, history, call, hand)
            if priority:
                possible_calls.add_call_with_priority(call, priority)
        return possible_calls


class Interpreter(object):
    def __init__(self):
        # Assuming SAYC for all sides.
        self.system = StandardAmericanYellowCard

    def create_history(self, call_history):
        history = History()
        viewer = call_history.position_to_call()

        for partial_history in call_history.ascending_partial_histories(step=1):
            selector = RuleSelector(self.system, history)

            call = partial_history.last_call()
            rule = selector.rule_for_call(call)
            # We can interpret bids we know how to make.
            constraints = NO_CONSTRAINTS
            annotations = []
            if rule:
                annotations.extend(rule.annotations)
                constraints = z3.And(rule.constraints_expr_for_call(history, call),
                                  selector.compile_constraints_for_call(history, call))
                # FIXME: We should validate the new constraints before saving them in the knowledge.
            history = history.extend_with(call, annotations, constraints)

        return history
