"""
:class:`EscapeValidator` verifies that no mutable data escapes
the region of its allocation.
"""

import functools
from pythonparser import algorithm, diagnostic
from .. import asttyped, types, builtins

def has_region(typ):
    return typ.fold(False, lambda accum, typ: accum or builtins.is_allocated(typ))

class Region:
    """
    A last-in-first-out allocation region. Tied to lexical scoping
    and is internally represented simply by a source range.

    :ivar range: (:class:`pythonparser.source.Range` or None)
    """

    def __init__(self, source_range=None):
        self.range = source_range

    def present(self):
        return bool(self.range)

    def includes(self, other):
        assert self.range
        assert self.range.source_buffer == other.range.source_buffer

        return self.range.begin_pos <= other.range.begin_pos and \
                    self.range.end_pos >= other.range.end_pos

    def intersects(self, other):
        assert self.range
        assert self.range.source_buffer == other.range.source_buffer

        return (self.range.begin_pos <= other.range.begin_pos <= self.range.end_pos and \
                    other.range.end_pos > self.range.end_pos) or \
               (other.range.begin_pos <= self.range.begin_pos <= other.range.end_pos and \
                    self.range.end_pos > other.range.end_pos)

    def contract(self, other):
        if not self.range:
            self.range = other.range

    def outlives(lhs, rhs):
        if lhs is None: # lhs lives forever
            return True
        elif rhs is None: # rhs lives forever, lhs does not
            return False
        else:
            assert not lhs.intersects(rhs)
            return lhs.includes(rhs)

    def __repr__(self):
        return "Region({})".format(repr(self.range))

class RegionOf(algorithm.Visitor):
    """
    Visit an expression and return the list of regions that must
    be alive for the expression to execute.
    """

    def __init__(self, env_stack, youngest_region):
        self.env_stack, self.youngest_region = env_stack, youngest_region

    # Liveness determined by assignments
    def visit_NameT(self, node):
        # First, look at stack regions
        for region in reversed(self.env_stack[1:]):
            if node.id in region:
                return region[node.id]

        # Then, look at the global region of this module
        if node.id in self.env_stack[0]:
            return None

        assert False

    # Value lives as long as the current scope, if it's mutable,
    # or else forever
    def visit_sometimes_allocating(self, node):
        if has_region(node.type):
            return self.youngest_region
        else:
            return None

    visit_BinOpT = visit_sometimes_allocating
    visit_CallT = visit_sometimes_allocating

    # Value lives as long as the object/container, if it's mutable,
    # or else forever
    def visit_accessor(self, node):
        if has_region(node.type):
            return self.visit(node.value)
        else:
            return None

    visit_AttributeT = visit_accessor
    visit_SubscriptT = visit_accessor

    # Value lives as long as the shortest living operand
    def visit_selecting(self, nodes):
        regions = [self.visit(node) for node in nodes]
        regions = list(filter(lambda x: x, regions))
        if any(regions):
            regions.sort(key=functools.cmp_to_key(Region.outlives), reverse=True)
            return regions[0]
        else:
            return None

    def visit_BoolOpT(self, node):
        return self.visit_selecting(node.values)

    def visit_IfExpT(self, node):
        return self.visit_selecting([node.body, node.orelse])

    def visit_TupleT(self, node):
        return self.visit_selecting(node.elts)

    # Value lives as long as the current scope
    def visit_allocating(self, node):
        return self.youngest_region

    visit_DictT = visit_allocating
    visit_DictCompT = visit_allocating
    visit_GeneratorExpT = visit_allocating
    visit_LambdaT = visit_allocating
    visit_ListT = visit_allocating
    visit_ListCompT = visit_allocating
    visit_SetT = visit_allocating
    visit_SetCompT = visit_allocating

    # Value lives forever
    def visit_immutable(self, node):
        assert not has_region(node.type)
        return None

    visit_NameConstantT = visit_immutable
    visit_NumT = visit_immutable
    visit_EllipsisT = visit_immutable
    visit_UnaryOpT = visit_immutable
    visit_CompareT = visit_immutable

    # Value is mutable, but still lives forever
    def visit_StrT(self, node):
        return None

    # Not implemented
    def visit_unimplemented(self, node):
        assert False

    visit_StarredT = visit_unimplemented
    visit_YieldT = visit_unimplemented
    visit_YieldFromT = visit_unimplemented


class AssignedNamesOf(algorithm.Visitor):
    """
    Visit an expression and return the list of names that appear
    on the lhs of assignment, directly or through an accessor.
    """

    def visit_NameT(self, node):
        return [node]

    def visit_accessor(self, node):
        return self.visit(node.value)

    visit_AttributeT = visit_accessor
    visit_SubscriptT = visit_accessor

    def visit_sequence(self, node):
        return functools.reduce(list.__add__, map(self.visit, node.elts))

    visit_TupleT = visit_sequence
    visit_ListT = visit_sequence

    def visit_StarredT(self, node):
        assert False


class EscapeValidator(algorithm.Visitor):
    def __init__(self, engine):
        self.engine = engine
        self.youngest_region = None
        self.env_stack = []
        self.youngest_env = None

    def _region_of(self, expr):
        return RegionOf(self.env_stack, self.youngest_region).visit(expr)

    def _names_of(self, expr):
        return AssignedNamesOf().visit(expr)

    def _diagnostics_for(self, region, loc, descr="the value of the expression"):
        if region:
            return [
                diagnostic.Diagnostic("note",
                    "{descr} is alive from this point...", {"descr": descr},
                    region.range.begin()),
                diagnostic.Diagnostic("note",
                    "... to this point", {},
                    region.range.end())
            ]
        else:
            return [
                diagnostic.Diagnostic("note",
                    "{descr} is alive forever", {"descr": descr},
                    loc)
            ]

    def visit_in_region(self, node, region, typing_env):
        try:
            old_youngest_region = self.youngest_region
            self.youngest_region = region

            old_youngest_env = self.youngest_env
            self.youngest_env = {}

            for name in typing_env:
                if has_region(typing_env[name]):
                    self.youngest_env[name] = Region(None) # not yet known
                else:
                    self.youngest_env[name] = None # lives forever
            self.env_stack.append(self.youngest_env)

            self.generic_visit(node)
        finally:
            self.env_stack.pop()
            self.youngest_env = old_youngest_env
            self.youngest_region = old_youngest_region

    def visit_ModuleT(self, node):
        self.visit_in_region(node, None, node.typing_env)

    def visit_FunctionDefT(self, node):
        self.youngest_env[node.name] = self.youngest_region
        self.visit_in_region(node, Region(node.loc), node.typing_env)

    def visit_ClassDefT(self, node):
        self.youngest_env[node.name] = self.youngest_region
        self.visit_in_region(node, Region(node.loc), node.constructor_type.attributes)

    # Only three ways for a pointer to escape:
    #   * Assigning or op-assigning it (we ensure an outlives relationship)
    #   * Returning it (we only allow returning values that live forever)
    #   * Raising it (we forbid allocating exceptions that refer to mutable data)¹
    #
    # Literals doesn't count: a constructed object is always
    # outlived by all its constituents.
    # Closures don't count: see above.
    # Calling functions doesn't count: arguments never outlive
    # the function body.
    #
    # ¹Strings are currently never allocated with a limited lifetime,
    # and exceptions can only refer to strings, so we don't actually check
    # this property. But we will need to, if string operations are ever added.

    def visit_assignment(self, target, value, is_aug_assign=False):
        value_region  = self._region_of(value) if not is_aug_assign else self.youngest_region

        # If this is a variable, we might need to contract the live range.
        if value_region is not None:
            for name in self._names_of(target):
                region = self._region_of(name)
                if region is not None:
                    region.contract(value_region)

        # The assigned value should outlive the assignee
        target_regions = [self._region_of(name) for name in self._names_of(target)]
        for target_region in target_regions:
            if not Region.outlives(value_region, target_region):
                if is_aug_assign:
                    target_desc = "the assignment target, allocated here,"
                else:
                    target_desc = "the assignment target"
                note = diagnostic.Diagnostic("note",
                    "this expression has type {type}",
                    {"type": types.TypePrinter().name(value.type)},
                    value.loc)
                diag = diagnostic.Diagnostic("error",
                    "the assigned value does not outlive the assignment target", {},
                    value.loc, [target.loc],
                    notes=self._diagnostics_for(target_region, target.loc,
                                                target_desc) +
                          self._diagnostics_for(value_region, value.loc,
                                                "the assigned value"))
                self.engine.process(diag)

    def visit_Assign(self, node):
        for target in node.targets:
            self.visit_assignment(target, node.value)

    def visit_AugAssign(self, node):
        if builtins.is_allocated(node.target.type):
            # If the target is mutable, op-assignment will allocate
            # in the youngest region.
            self.visit_assignment(node.target, node.value, is_aug_assign=True)

    def visit_Return(self, node):
        region = self._region_of(node.value)
        if region:
            note = diagnostic.Diagnostic("note",
                "this expression has type {type}",
                {"type": types.TypePrinter().name(node.value.type)},
                node.value.loc)
            diag = diagnostic.Diagnostic("error",
                "cannot return a mutable value that does not live forever", {},
                node.value.loc, notes=self._diagnostics_for(region, node.value.loc) + [note])
            self.engine.process(diag)
