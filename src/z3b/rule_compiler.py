# Copyright (c) 2013 The SAYCBridge Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from core.call import Call
from itertools import chain
from z3b import enum
from z3b import model
from z3b import ordering
from z3b.constraints import Constraint
from z3b.preconditions import implies_artificial, annotations
import z3


categories = enum.Enum(
    "Relay",
    "Gadget",
    "NoTrumpSystem",
    "Default",
    "Natural",
    "LawOfTotalTricks",
    "NaturalPass",
    "DefaultPass",
)


rule_order = ordering.Ordering()


# This is a public interface from RuleGenerators to the rest of the system.
# This class knows nothing about the DSL.
class CompiledRule(object):
    def __init__(self, rule, preconditions, known_calls, shared_constraints, annotations, constraints, default_priority, conditional_priorities_per_call, priorities_per_call):
        self.dsl_rule = rule
        self.preconditions = preconditions
        self.known_calls = known_calls
        self.shared_constraints = shared_constraints
        self._annotations = annotations
        self.constraints = constraints
        self.default_priority = default_priority
        self.conditional_priorities_per_call = conditional_priorities_per_call
        self.priorities_per_call = priorities_per_call

    def requires_planning(self, history):
        return self.dsl_rule.requires_planning

    def annotations_for_call(self, call):
        if self.dsl_rule.annotations_per_call:
            per_call_annotations = self.dsl_rule.annotations_per_call.get(call.name)
            if per_call_annotations:
                return self._annotations | set(RuleCompiler._ensure_list(per_call_annotations))
        return self._annotations

    @property
    def name(self):
        return self.dsl_rule.name()

    def __str__(self):
        return self.name

    def __repr__(self):
        return "CompiledRule(%s)" % self.dsl_rule.name()

    # FIXME: This exists for compatiblity with KBB's Rule interface and is used by bidder_handler.py
    def explanation_for_bid(self, call):
        return None

    # FIXME: This exists for compatiblity with KBB's Rule interface and is used by bidder_handler.py
    def sayc_page_for_bid(self, call):
        return None

    def _fits_preconditions(self, history, call, expected_call=None):
        try:
            for precondition in self.preconditions:
                if not precondition.fits(history, call):
                    if call == expected_call and expected_call in self.known_calls:
                        print " %s failed: %s" % (self, precondition)
                    return False
        except Exception, e:
            print "Exception evaluating preconditions for %s" % self.name
            raise
        return True

    def calls_over(self, history, expected_call=None):
        for call in history.legal_calls.intersection(self.known_calls):
            if self._fits_preconditions(history, call, expected_call):
                yield self.dsl_rule.category, call

    def _constraint_exprs_for_call(self, history, call):
        exprs = []
        per_call_constraints, _ = self.per_call_constraints_and_priority(call)
        if per_call_constraints:
            exprs.extend(RuleCompiler.exprs_from_constraints(per_call_constraints, history, call))
        exprs.extend(RuleCompiler.exprs_from_constraints(self.shared_constraints, history, call))
        return exprs

    def meaning_of(self, history, call):
        exprs = self._constraint_exprs_for_call(history, call)
        per_call_conditionals = self.conditional_priorities_per_call.get(call.name)
        if per_call_conditionals:
            for condition, priority in per_call_conditionals:
                condition_exprs = RuleCompiler.exprs_from_constraints(condition, history, call)
                yield priority, z3.And(exprs + condition_exprs)

        for condition, priority in self.dsl_rule.conditional_priorities:
            condition_exprs = RuleCompiler.exprs_from_constraints(condition, history, call)
            yield priority, z3.And(exprs + condition_exprs)

        _, priority = self.per_call_constraints_and_priority(call)
        assert priority
        yield priority, z3.And(exprs)

    # constraints accepts various forms including:
    # constraints = { '1H': hearts > 5 }
    # constraints = { '1H': (hearts > 5, priority) }
    # constraints = { ('1H', '2H'): hearts > 5 }

    # FIXME: Should we split this into two methods? on for priority and one for constraints?
    def per_call_constraints_and_priority(self, call):
        constraints_tuple = self.constraints.get(call.name)
        try:
            list(constraints_tuple)
        except TypeError:
            priority = self.priorities_per_call.get(call.name, self.default_priority)
            constraints_tuple = (constraints_tuple, priority)
        assert len(constraints_tuple) == 2
        # FIXME: Is it possible to not end up with a priority anymore?
        assert constraints_tuple[1], "" + self.name + " is missing priority"
        return constraints_tuple


class RuleCompiler(object):
    @classmethod
    def exprs_from_constraints(cls, constraints, history, call):
        if not constraints:
            return [model.NO_CONSTRAINTS]

        if isinstance(constraints, Constraint):
            return [constraints.expr(history, call)]

        if isinstance(constraints, z3.ExprRef):
            return [constraints]

        return chain.from_iterable([cls.exprs_from_constraints(constraint, history, call) for constraint in constraints])

    @classmethod
    def _collect_from_ancestors(cls, dsl_class, property_name):
        getter = lambda ancestor: getattr(ancestor, property_name, [])
        # The DSL expects that parent preconditions, etc. apply before child ones.
        return map(getter, reversed(dsl_class.__mro__))

    @classmethod
    def _ensure_list(cls, value_or_list):
        if value_or_list and not hasattr(value_or_list, '__iter__'):
            return [value_or_list]
        return value_or_list

    @classmethod
    def _joined_list_from_ancestors(cls, dsl_class, property_name):
        values_from_ancestors = cls._collect_from_ancestors(dsl_class, property_name)
        mapped_values = map(cls._ensure_list, values_from_ancestors)
        return list(chain.from_iterable(mapped_values))

    @classmethod
    def _compile_known_calls(cls, dsl_class, constraints, priorities_per_call):
        if dsl_class.call_names:
            call_names = cls._ensure_list(dsl_class.call_names)
        elif priorities_per_call:
            call_names = priorities_per_call.keys()
            assert dsl_class.shared_constraints or priorities_per_call.keys() == constraints.keys()
        else:
            call_names = constraints.keys()
        assert call_names, "%s: call_names or priorities_per_call or constraints map is required." % dsl_class.__name__
        return map(Call.from_string, call_names)

    @classmethod
    def _flatten_tuple_keyed_dict(cls, original_dict):
        flattened_dict  = {}
        for tuple_key, value in original_dict.iteritems():
            if hasattr(tuple_key, '__iter__'):
                for key in tuple_key:
                    assert key not in flattened_dict, "Key (%s) was listed twice in %s" % (key, original_dict)
                    flattened_dict[key] = value
            else:
                assert tuple_key not in flattened_dict, "Key (%s) was listed twice in %s" % (tuple_key, original_dict)
                flattened_dict[tuple_key] = value
        return flattened_dict

    @classmethod
    def _compile_annotations(cls, dsl_class):
        compiled_set = set(cls._joined_list_from_ancestors(dsl_class, 'annotations'))
        # FIXME: We should probably assert that no more than one of the "implies_artificial"
        # annotations are in this set at once.  Those all have distinct meanings.
        if implies_artificial.intersection(compiled_set):
            compiled_set.add(annotations.Artificial)
        return compiled_set

    @classmethod
    def _validate_rule(cls, dsl_class):
        # Rules have to apply some constraints to the hand.
        assert dsl_class.constraints or dsl_class.shared_constraints, "" + dsl_class.name() + " is missing constraints"
        # conditional_priorities doesn't work with self.constraints
        assert not dsl_class.conditional_priorities or not dsl_class.constraints
        assert not dsl_class.conditional_priorities or dsl_class.call_names
        # FIXME: We should also walk the class and assert that no unexpected properties are found.
        allowed_keys = set([
            "annotations",
            "annotations_per_call",
            "call_names",
            "category",
            "conditional_priorities",
            "constraints",
            "preconditions",
            "priority",
            "priorities_per_call",
            "requires_planning",
            "shared_constraints",
            "conditional_priorities_per_call",
        ])
        properties = dsl_class.__dict__.keys()
        public_properties = filter(lambda p: not p.startswith("_"), properties)
        unexpected_properties = set(public_properties) - allowed_keys
        assert not unexpected_properties, "%s defines unexpected properties: %s" % (dsl_class, unexpected_properties)

    @classmethod
    def _default_priority(cls, dsl_rule):
        if dsl_rule.priority:
            return dsl_rule.priority
        return dsl_rule # Use the class as the default priority.

    @classmethod
    def compile(cls, dsl_rule):
        cls._validate_rule(dsl_rule)
        constraints = cls._flatten_tuple_keyed_dict(dsl_rule.constraints)
        priorities_per_call = cls._flatten_tuple_keyed_dict(dsl_rule.priorities_per_call)
        # Unclear if compiled results should be memoized on the rule?
        return CompiledRule(dsl_rule,
            known_calls=cls._compile_known_calls(dsl_rule, constraints, priorities_per_call),
            annotations=cls._compile_annotations(dsl_rule),
            preconditions=cls._joined_list_from_ancestors(dsl_rule, 'preconditions'),
            shared_constraints=cls._joined_list_from_ancestors(dsl_rule, 'shared_constraints'),
            constraints=constraints,
            default_priority=cls._default_priority(dsl_rule),
            conditional_priorities_per_call=cls._flatten_tuple_keyed_dict(dsl_rule.conditional_priorities_per_call),
            priorities_per_call=priorities_per_call,
        )


# The rules of SAYC are all described in terms of Rule.
# These classes exist to support the DSL and make it easy to concisely express
# the conventions of SAYC.
class Rule(object):
    # FIXME: Consider splitting call_preconditions out from preconditions
    # for preconditions which only operate on the call?
    preconditions = [] # Auto-collects from parent classes
    category = categories.Default # Intra-bid priority
    requires_planning = False

    # All possible call names must be listed.
    # Having an up-front list makes the bidder's rule-evaluation faster.
    call_names = None # Required.

    constraints = {}
    shared_constraints = [] # Auto-collects from parent classes
    annotations = []
    conditional_priorities = []
    # FIXME: conditional_priorities_per_call is a stepping-stone to allow us to
    # write more conditional priorities and better understand what rules we should
    # develop to generate them.  The syntax is:
    # {'1C': [(condition, priority), (condition, priority)]}
    conditional_priorities_per_call = {}
    annotations_per_call = {}
    priority = None
    priorities_per_call = {}

    def __init__(self):
        assert False, "Rule objects should be compiled into EngineRule objects instead of instantiating them."

    @classmethod
    def name(cls):
        return cls.__name__

    def __repr__(self):
        return "%s()" % self.name
