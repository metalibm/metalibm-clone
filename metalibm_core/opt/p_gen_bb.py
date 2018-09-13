# -*- coding: utf-8 -*-
# optimization pass to promote a scalar/vector DAG into vector registers

###############################################################################
# This file is part of metalibm (https://github.com/kalray/metalibm)
###############################################################################
# MIT License
#
# Copyright (c) 2018 Kalray
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
###############################################################################

from metalibm_core.core.ml_operations import (
    Statement, ConditionBlock, Return, Loop,
    Variable, ReferenceAssign, ML_LeafNode,
    ControlFlowOperation, EmptyOperand,
    Constant
)

from metalibm_core.core.passes import FunctionPass, Pass, LOG_PASS_INFO
from metalibm_core.core.bb_operations import (
    BasicBlockList,
    ConditionalBranch, UnconditionalBranch, BasicBlock,
    PhiNode
)

from metalibm_core.utility.log_report import Log



def transform_cb_to_bb(bb_translator, optree, fct=None, fct_group=None, memoization_map=None): 
    """ Transform a ConditionBlock node to a list of BasicBlock
        returning the entry block as result.

        @param bb_translator generic basic-block translator object
                             which can be called recursively on CB sub basic
                             blocks
        @param optree input operation tree
        @return entry BasicBlock to the ConditionBlock translation """
    # get current unfinished basic block (CB:) from top of stack
    #
    # When encoutering
    # if <cond>:
    #       if_branch
    # else:
    #       else_branch
    #
    # generates
    #  CB:
    #    ....
    #    br BB0
    #   BB0:
    #      cb <cond>, if_block
    #   if_block:
    #       if_branch
    #       br next
    #   else_block:     (if any)
    #       else_branch
    #       br next
    #   next:
    #
    #  next is pushed on top of stack
    entry_bb =  bb_translator.get_current_bb()
    # at this point we can force addition to bb_list
    # as we known entry_bb will not be empty because of
    # ConditionalBranch appended at the end of this function
    bb_translator.pop_current_bb(force_add=True)

    cond = optree.get_input(0)
    if_branch = optree.get_input(1)
    if optree.get_input_num() > 2:
        else_branch = optree.get_input(2)
    else:
        else_branch = None
    # create new basic block to generate if branch
    if_entry = bb_translator.push_new_bb()
    if_bb = bb_translator.execute_on_optree(if_branch, fct, fct_group, memoization_map)
    # pop bb created for if-branch
    if_end = bb_translator.pop_current_bb(force_add=True)

    # push new bb for else-branch or next
    if not else_branch is None:
        else_entry = bb_translator.push_new_bb()
        else_bb = bb_translator.execute_on_optree(else_branch, fct, fct_group, memoization_map)
        # end of bb for else branch
        else_end = bb_translator.pop_current_bb(force_add=True)
        # new bb for next block (only if else block not empty)
        next_bb = bb_translator.push_new_bb()
        bb_translator.add_to_bb(else_end, UnconditionalBranch(next_bb))
    else:
        else_entry = bb_translator.push_new_bb()

    next_bb = bb_translator.get_current_bb()
    # adding end of block instructions
    cb = ConditionalBranch(cond, if_entry, else_entry)
    bb_translator.add_to_bb(entry_bb, cb)
    # adding end of if block
    bb_translator.add_to_bb(if_end, UnconditionalBranch(next_bb))
    return entry_bb

def transform_loop_to_bb(bb_translator, optree, fct=None, fct_group=None, memoization_map=None):
    """ Transform a Loop node to a list of BasicBlock
        returning the entry block as result.

        @param bb_translator generic basic-block translator object
                             which can be called recursively on Loop's sub basic
                             blocks
        @param optree input operation tree
        @return entry BasicBlock to the Loop translation """
    init_statement = optree.get_input(0)
    loop_cond = optree.get_input(1)
    loop_body = optree.get_input(2)

    # loop pre-header
    init_block = bb_translator.execute_on_optree(init_statement, fct, fct_group, memoization_map)

    # building loop header
    bb_translator.push_new_bb("loop_header")
    loop_header_entry = bb_translator.get_current_bb()
    bb_translator.add_to_bb(loop_header_entry, loop_cond)
    bb_translator.execute_on_optree(loop_cond, fct, fct_group, memoization_map)
    assert bb_translator.pop_current_bb()

    # building loop body
    bb_translator.push_new_bb("loop_body")
    body_bb = bb_translator.execute_on_optree(loop_body, fct, fct_group, memoization_map)
    bb_translator.add_to_bb(body_bb, UnconditionalBranch(loop_header_entry))
    assert bb_translator.pop_current_bb()
    bb_translator.push_new_bb("loop_exit")
    next_bb = bb_translator.get_current_bb()

    # loop header branch generation
    loop_branch = ConditionalBranch(loop_cond, body_bb, next_bb)
    bb_translator.add_to_bb(loop_header_entry, loop_branch)

    # lopp pre-header to header branch generation
    bb_translator.add_to_bb(init_block, UnconditionalBranch(loop_header_entry))

    # returning entry block
    return init_block

def get_recursive_op_graph_vars(node, memoization_map=None):
    memoization_map = {} if memoization_map is None else memoization_map
    if node in memoization_map:
        return memoization_map[node]
    if isinstance(node, Variable):
        result = set([node])
        memoization_map[node] = result 
        return result
    elif isinstance(node, ML_LeafNode):
        result = set()
        memoization_map[node] = result
        return result
    else:
        result = set()
        memoization_map[node] = result
        result = result.union(sum([list(get_recursive_op_graph_vars(op, memoization_map)) for op in node.get_inputs()], [])) 
        memoization_map[node] = result
        return result

def copy_up_to_variables(node):
    """ Create a copy of nodes without copying variables """
    var_list = get_recursive_op_graph_vars(node)
    return node.copy(dict((var, var) for var in var_list))


def sort_node_by_bb(bb_list):
    """ build a dictionnary mapping each node
        to a basic-block, also checks that each node
        only appears in a basic block """
    bb_map = {}
    for bb in bb_list.inputs:
        working_set = set()
        processed_nodes = set()
        for node in bb.get_inputs():
            working_set.add(node)
        while len(working_set) > 0:
            node = working_set.pop()
            if not node in processed_nodes:
                processed_nodes.add(node)
                if node in bb_map and bb_map[node] != bb:
                    if isinstance(node, Variable):
                        pass
                    elif isinstance(node, Constant):
                        # a constant may be copied
                        node = node.copy()
                    else:
                        # conflicting bb, copying until variable
                        node = copy_up_to_variables(node)
                        #Log.report(Log.Error, "bb conflict for node: \n{}, BB0: \n{}\n, BB1: \n{}", node, bb, bb_map[node])
                bb_map[node] = bb
                if not isinstance(node, ML_LeafNode) and not isinstance(node, ControlFlowOperation):
                    for op in node.get_inputs():
                        working_set.add(op)
                elif isinstance(node, ConditionalBranch):
                    working_set.add(node.get_input(0))

    return bb_map

class CFGEdge(object):
    def __init__(self, src, dst):
        self.src = src
        self.dst = dst


class BasicBlockGraph:
    """ Encapsulate several structure related
        to a basic block control flow graphs """
    def __init__(self, root, bb_list):
        # list of basic blocks
        self.root = root
        self.bb_list = bb_list
        self._bb_map = None
        self._variable_list = None
        self._variable_defs = None
        self._dominance_frontier_map = None
        self._dominator_map = None
        self._immediate_dominator_map = None
        self._cfg_edges = None
        self._dominator_tree = None

    @property
    def dominator_tree(self):
        if self._dominator_tree is None:
            self._dominator_tree = build_dominator_tree(self.immediate_dominator_map, self.root)
        return self._dominator_tree

    @property
    def bb_map(self):
        """ Basic Block map with lazy evaluation """
        if self._bb_map is None:
            self._bb_map = sort_node_by_bb(self.bb_list)
        return self._bb_map

    @property
    def variable_list(self):
        if self._variable_list is None:
            self._variable_list, self._variable_defs = build_variable_list_and_defs(self.bb_list, self.bb_map)
        return self._variable_list

    @property
    def variable_defs(self):
        if self._variable_defs is None:
            self._variable_list, self._variable_defs = build_variable_list_and_defs(self.bb_list, self.bb_map)
        return self._variable_defs

    @property
    def dominance_frontier_map(self):
        if self._dominance_frontier_map is None:
            self._dominance_frontier_map = get_dominance_frontiers(self)
        return self._dominance_frontier_map

    @property
    def cfg_edges(self):
        if self._cfg_edges is None:
            self._cfg_edges = build_cfg_edges_set(self.bb_list)
        return self._cfg_edges

    @property
    def dominator_map(self):
        if self._dominator_map is None:
            self._dominator_map = build_dominator_map(self)
        return self._dominator_map

    @property
    def immediate_dominator_map(self):
        if self._immediate_dominator_map is None:
            self._immediate_dominator_map = build_immediate_dominator_map(self.dominator_map, self.bb_list, self.cfg_edges)
        return self._immediate_dominator_map

    def op_dominates(self, op0, op1):
        """ test if op0 dominates op1 """
        return self.bb_map[op0] in self.dominator_map[self.bb_map[op1]]
            


def build_cfg_edges_set(bb_list):
    cfg_edges = set()
    for bb in bb_list.inputs:
        last_op = bb.get_inputs()[-1]
        if isinstance(last_op, UnconditionalBranch):
            dst = last_op.get_input(0)
            cfg_edges.add(CFGEdge(bb, dst))
        elif isinstance(last_op, ConditionalBranch):
            true_dst = last_op.get_input(1)
            false_dst = last_op.get_input(2)
            cfg_edges.add(CFGEdge(bb, true_dst))
            cfg_edges.add(CFGEdge(bb, false_dst))
    return cfg_edges

def build_predominance_map_list(cfg_edges):
    predominance_map_list = {}
    for edge in cfg_edges:
        if not edge.dst in predominance_map_list:
            predominance_map_list[edge.dst] = []
        predominance_map_list[edge.dst].append(edge.src)
    return predominance_map_list

def build_dominator_map(bbg):
    """ Build a dict which associates to each node N
        a list of nodes ominated by N.
        (by definition this list contains at least N, since each node
        dominates itself) """
    predominance_map_list = build_predominance_map_list(bbg.cfg_edges)
    # building dominator map for each node
    dominator_map = {}
    for bb in bbg.bb_list.inputs:
        dominator_map[bb] = set([bb])
    while True:
        change = False
        # dom(n) = fix point of intersect(dom(x), x in predecessor(n)) U {n}
        for bb in predominance_map_list:
            dom = set.union(
                set([bb]),
                set.intersection(
                    *tuple(dominator_map[pred] for pred in predominance_map_list[bb])
                )
            )
            if not bb in dominator_map:
                Log.report(Log.Error, "following bb was not in dominator_map: {}", bb)
            if dom != dominator_map[bb]:
                change = True
            dominator_map[bb] = dom
        if not change:
            break
    return dominator_map

def build_immediate_dominator_map(dominator_map, bb_list, cfg_edges):
    """ Build a dictionnary associating the immediate dominator of each
        basic-block to this BB """
    # looking for immediate dominator
    immediate_dominator_map = {}
    for bb in bb_list.inputs:
        #print "dominator_map of {} is {}".format(bb.get_tag(), [d.get_tag() for d in dominator_map[bb]])
        for dom in dominator_map[bb]:
            # skip self node
            if dom is bb: continue
            #print "considering {} as potential imm dom for {}".format(dom.get_tag(), bb.get_tag())
            is_imm = True
            for other_dom in dominator_map[bb]:
                if other_dom is dom or other_dom is bb: continue
                if dom in dominator_map[other_dom]:
                    #print "{} is in dominator map of {}".format(dom.get_tag(), other_dom.get_tag())
                    is_imm = False
                    break
            if is_imm:
                immediate_dominator_map[bb] = dom
                break
        if not bb in immediate_dominator_map:
            Log.report(Log.Info, "could not find immediate dominator for: \n{}", bb)
            #print "imm dom not found for ", bb.get_str()
        #else:
            #print "{}'s imm dom is {}".format(bb.get_tag(), immediate_dominator_map[bb].get_tag())
        # does not work for root BB
        # assert bb in immediate_dominator_map
    return immediate_dominator_map


def does_not_strictly_dominate(dominator_map, x, y):
    if x == y:
        return True
    elif x in dominator_map[y]:
        return False
    else:
        return True


def get_dominance_frontiers(bbg):
    # build BB's conftrol flow graph
    dominance_frontier = {}
    cfg_edges = build_cfg_edges_set(bbg.bb_list)
    # dominator_map = build_dominator_map(cfg_edges)
    # immediate_dominator_map = build_immediate_dominator_map(dominator_map, bb_list, cfg_edges)
    for edge in bbg.cfg_edges:
        x = edge.src
        while does_not_strictly_dominate(bbg.dominator_map, x, edge.dst):
            if not x in dominance_frontier:
                dominance_frontier[x] = set()
            dominance_frontier[x].add(edge.dst)
            if not x in bbg.immediate_dominator_map:
                print x.get_str(depth=2, display_precision=True)
            x = bbg.immediate_dominator_map[x]
    for x in dominance_frontier:
        Log.report(Log.Verbose, "dominance_frontier of {} is {}", x.get_tag(), [bb.get_tag() for bb in dominance_frontier[x]])
    return dominance_frontier

def build_variable_list_and_defs(bb_list, bb_map):
    """ Build the list of Variable node appearing in the
        basic block list @p bb_list, also build a dict
        listing all definition of each variable """
    # listing variable and their definition
    variable_list = set()
    variable_defs = {}
    processed_nodes = set()
    working_set = set()
    for bb in bb_list.inputs:
        for node in bb.get_inputs():
            working_set.add(node)
    while len(working_set) > 0:
        node = working_set.pop()
        if not node in processed_nodes:
            processed_nodes.add(node)
            if isinstance(node, Variable):
                variable_list.add(node)
                if not node in variable_defs:
                    variable_defs[node] = set()
            elif isinstance(node, ReferenceAssign):
                var = node.get_input(0)
                if not var in variable_defs:
                    variable_defs[var] = set()
                variable_defs[var].add(bb_map[node])
            if not isinstance(node, ML_LeafNode):
                for op in node.get_inputs():
                    working_set.add(op)
    return variable_list, variable_defs


def phi_node_insertion(bbg):
    """ Perform first phase of SSA translation: insert phi-node
        where required """

    for v in bbg.variable_list:
        F = set() # set of bb where phi-node were added
        W = set() # set of bb which contains definition of v
        try:
            def_list = bbg.variable_defs[v]
        except KeyError as e:
            Log.report(Log.Error, "variable {} has no defs", v, error=e)
        for bb in def_list:
            #bb = bb_map[op]
            W.add(bb)
        while len(W) > 0:
            Log.report(Log.Verbose, "len(W)={}", len(W))
            x = W.pop()
            try:
                df = bbg.dominance_frontier_map[x]
            except KeyError:
                Log.report(Log.Verbose, "could not fprintind dominance frontier for {}".format(x.get_tag()))
                df = []
            Log.report(Log.Verbose, "df: {}", df)
            for y in df:
                Log.report(Log.Verbose, "y: {}", y)
                if not y in F:
                    # add phi-node x <- at entry of y
                    # TODO: manage more than 2 predecessor for v
                    phi_fct = PhiNode(v, v, None, v, None) 
                    y.push(phi_fct)
                    # adding  phi funciton to bb map
                    bbg.bb_map[phi_fct] = y
                    F.add(y)
                    if not y in def_list:
                        W.add(y)


class DominatorTree(dict):
    def __init__(self, root=None):
        dict.__init__(self)
        self.root = root


def build_dominator_tree(immediate_dominator_map, root):
    # immediate_dominator_map Node -> immediate dominator (unique)
    dom_tree = DominatorTree(root)
    for imm_dominated in immediate_dominator_map:
        imm_dominator = immediate_dominator_map[imm_dominated]
        if not imm_dominator in dom_tree:
            dom_tree[imm_dominator] = []
        dom_tree[imm_dominator].append(imm_dominated)
    return dom_tree


def get_var_used_by_non_phi(op):
    if isinstance(op, PhiNode):
        return []
    elif isinstance(op, Variable):
        return [op]
    elif isinstance(op, ML_LeafNode):
        return []
    elif isinstance(op, ReferenceAssign):
        return sum([get_var_used_by_non_phi(op_input) for op_input in op.get_inputs()[1:]], [])
    else:
        return sum([get_var_used_by_non_phi(op_input) for op_input in op.get_inputs()], [])

def update_used_var(op, old_var, new_var, memoization_map=None):
    """ Change occurence of @p old_var by @p new_var in Operation @p op """
    assert not op is None
    if memoization_map is None:
        memoization_map = {}

    Log.report(Log.Verbose, "update_var of op {} from {} to {}", op, old_var, new_var)

    if new_var is None:
        Log.report(Log.Verbose, "skipping update_used_var as new_var is {}", new_var)
        return

    if op in memoization_map:
        return memoization_map[op]
    elif isinstance(op, BasicBlock):
        Log.report(Log.Verbose, "skipping bb")
        return None
    elif op == old_var:
        if new_var is None:
            Log.report(Log.Error, "trying to update var {} to {}, maybe variable was used before def", old_var, new_var)
        memoization_map[op] = new_var
        return new_var
    elif isinstance(op, ML_LeafNode):
        memoization_map[op] = op
        return op
    else:
        # in-place swap
        memoization_map[op] = op
        for index, op_input in enumerate(op.get_inputs()):
            if isinstance(op, ReferenceAssign) and index == 0:
                # first input is in fact an output
                continue
            if op_input == old_var:
                if new_var is None:
                    Log.report(Log.Error, "trying to update var {} to {}, maybe variable was used before def", old_var, new_var)
                op.set_input(index, new_var)
            else:
                # recursion
                update_used_var(op_input, old_var, new_var, memoization_map)
        return op

def update_def_var(op, var, vp):
    """ Update @p var which should be the variable defined by @p
        and replace it by @p vp """
    # TODO: manage sub-assignation cases
    assert isinstance(op, ReferenceAssign) or isinstance(op, PhiNode)
    assert op.get_input(0) is var
    assert not vp is None
    Log.report(Log.Verbose, "updating var def in {} from {} to {}", op, var, vp)
    op.set_input(0, vp)

def updating_reaching_def(bbg, reaching_def, var, op):
    # search through chain of definitions for var until it find
    # the closest definition that dominates op, then update
    # reaching_def[var] in-place with this definition
    current = reaching_def[var]
    while not (current == None or bbg.op_dominates(bbg.variable_defs[current], op)):
        current = reaching_def[current]
    if current is None:
        Log.report(Log.Verbose, "skipping updating_reaching_def from {} to {}", reaching_def[var], current)
        return False
    else:
        Log.report(Log.Verbose, "reaching_def of var {} from op {} updated from {} to {}", var, op, reaching_def[var], current)
        change = (current != reaching_def[var])
        reaching_def[var] = current
        return change


def get_var_def_by_op(op):
    """ return the list of variable defined by op (if any) """
    if isinstance(op, PhiNode):
        return [op.get_input(0)]
    elif isinstance(op, ReferenceAssign):
        return [op.get_input(0)]
    else:
        return []

def get_phi_list_in_bb_successor(bb):
    """ """
    phi_list = []
    for successor in bb.successors:
        for op in successor.get_inputs():
            if isinstance(op, PhiNode):
                phi_list.append(op)
    return phi_list

def update_indexed_used_var(op, index, old_var, new_var, pred_bb):
    if new_var is None:
        Log.report(Log.Verbose, "skipping update_indexed_used_var from {} to {}", old_var, new_var)
    else:
        Log.report(Log.Verbose, "updating indexed used var in phi from {} to {}", old_var, new_var)
        op.set_input(index, new_var)
        op.set_input(index + 1, pred_bb)
        
def get_indexed_var_used_by_phi(phinode):
    assert isinstance(phinode, PhiNode)
    return [(2 * index + 1 , op, phinode.get_input(2 * index + 2)) for index, op in enumerate(phinode.get_inputs()[1::2]) if isinstance(op, Variable)]
        

def variable_renaming(bbg):
    """ Perform second stage of SSA transformation: variable renaming """
    # dict Variable -> definition
    reaching_def = dict((var, None) for var in bbg.variable_list)

    var_index = {}
    def new_var_index(var):
        if not var in var_index:
            var_index[var] = 0
        new_index = var_index[var]
        var_index[var] = new_index + 1
        return new_index

    def rec_bb_processing(bb):
        Log.report(Log.Verbose, "processing bb {}", bb)
        for op in bb.get_inputs():
            Log.report(Log.Verbose, "processing op {}", op)
            if not isinstance(op, PhiNode):
                for var in get_var_used_by_non_phi(op):
                    Log.report(Log.Verbose, "processing var {} used by non-phi node", var)
                    updating_reaching_def(bbg, reaching_def, var, op)
                    Log.report(Log.Verbose, "updating var from {} to {} used by non-phi node", var, reaching_def[var])
                    update_used_var(op, var, reaching_def[var])
            for var in get_var_def_by_op(op):
                updating_reaching_def(bbg, reaching_def, var, op)
                vp = Variable("%s_%d" % (var.get_tag(), new_var_index(var))) # TODO: tag
                update_def_var(op, var, vp)
                reaching_def[vp] = reaching_def[var]
                reaching_def[var] = vp
                bbg.variable_defs[vp] = op
        Log.report(Log.Verbose, "processing phi in successor")
        for phi in get_phi_list_in_bb_successor(bb):
            for index, var, var_bb in get_indexed_var_used_by_phi(phi):
                Log.report(Log.Verbose, "processing operand #{} of phi: {}, var_bb is {}", index, var, var_bb)
                if not isinstance(var_bb, EmptyOperand):
                    continue
                # updating_reaching_def(bbg, reaching_def, var, phi)
                update_indexed_used_var(phi, index, var, reaching_def[var], bb)
                break
        # finally traverse sub-tree
        if bb in bbg.dominator_tree:
            for child in bbg.dominator_tree[bb]:
                rec_bb_processing(child)

    # processing dominator tree in depth-first search pre-order
    rec_bb_processing(bbg.dominator_tree.root)


class Pass_GenerateBasicBlock(FunctionPass):
    """ pre-Linearize operation tree to basic blocks
        Control flow construction are transformed into linked basic blocks
        Dataflow structure of the operation graph is kept for unambiguous
        construct (basically non-control flow nodes) """
    pass_tag = "gen_basic_block"
    def __init__(self, target, description = "generate basic-blocks pass"):
        FunctionPass.__init__(self, description, target)
        self.memoization_map = {}
        self.top_bb_list = None
        self.current_bb_stack = []
        self.bb_tag_index = 0

    def set_top_bb_list(self, bb_list):
        """ define the top basic block and reset the basic block stack so it
            only contains this block """
        self.top_bb_list = bb_list
        self.current_bb_stack = [self.top_bb_list.entry_bb]
        return self.top_bb_list

    def push_to_current_bb(self, node):
        """ add a new node at the end of the current basic block
            which is the topmost on the BB stack """
        assert len(self.current_bb_stack) > 0
        self.current_bb_stack[-1].push(node)

    def get_new_bb_tag(self, tag):
        if tag is None:
            tag = "bb_%d" % self.bb_tag_index
            self.bb_tag_index +=1 
        return tag

    @property
    def top_bb(self):
        return self.current_bb_stack[-1]

    def push_new_bb(self, tag=None):
        # create the new basic-block
        tag = self.get_new_bb_tag(tag)
        new_bb = BasicBlock(tag=tag)

        # register new basic-block at the end of the current list
        # self.top_bb_list.add(new_bb)

        # set the new basic block at the top of the bb stack
        print("appending new bb to stack: ", new_bb.get_tag())
        self.current_bb_stack.append(new_bb)
        return new_bb

    def add_to_bb(self, bb, node):
        if not bb.final and (bb.empty or not isinstance(bb.get_input(-1), ControlFlowOperation)):
            bb.add(node)

    def pop_current_bb(self, force_add=False):
        """ remove the topmost basic block from the BB stack
            and add it to the list of basic blocks """
        if len(self.current_bb_stack) >= 1:
            top_bb = self.current_bb_stack[-1]
            print "poping top_bb {} from stack ".format(top_bb.get_tag())
            # TODO/FIXME: fix bb regsiterting in top_bb_list testing
            if not top_bb.empty or force_add:
                top_bb = self.current_bb_stack.pop(-1)
                self.top_bb_list.add(top_bb)
                return top_bb
        print "   top_bb was empty"
        return None

    def flush_bb_stack(self):
        # flush all but last bb (which is root/main)
        while len(self.current_bb_stack) > 1:
            assert self.pop_current_bb()
        print "bb_stack after flush: ", [bb.get_tag() for bb in self.current_bb_stack]

    def get_current_bb(self):
        return self.current_bb_stack[-1]

    def execute_on_optree(self, optree, fct=None, fct_group=None, memoization_map=None): 
        """ return the head basic-block, i.e. the entry BB for the current node
            implementation """
        assert not isinstance(optree, BasicBlock)
        entry_bb =  self.get_current_bb()
        if isinstance(optree, ConditionBlock):
            entry_bb = transform_cb_to_bb(self, optree)
        elif isinstance(optree, Loop):
            entry_bb = transform_loop_to_bb(self, optree)
        elif isinstance(optree, Return):
            # Return must be processed separately as it finishes a basic block
            self.push_to_current_bb(optree)
            self.get_current_bb().final = True

        elif isinstance(optree, Statement):
            for op in optree.get_inputs():
                self.execute_on_optree(op, fct, fct_group, memoization_map)
        else:
            self.push_to_current_bb(optree)
        return entry_bb


    def execute_on_function(self, fct, fct_group):
        """ """
        Log.report(Log.Info, "executing pass {} on fct {}".format(
            self.pass_tag, fct.get_name()))
        optree = fct.get_scheme()
        memoization_map = {}
        new_bb = BasicBlock(tag="main")
        bb_list = BasicBlockList(tag="main")
        bb_list.entry_bb = new_bb
        top_bb_list = self.set_top_bb_list(bb_list)
        last_bb = self.execute_on_optree(optree, fct, fct_group, memoization_map)
        # pop last basic-block (to add it to the list)
        print "last_bb: ", last_bb.get_tag()
        print [bb.get_tag() for bb in top_bb_list.get_inputs()]
        self.flush_bb_stack()
        print [bb.get_tag() for bb in top_bb_list.get_inputs()]
        fct.set_scheme(top_bb_list)


class Pass_BBSimplification(FunctionPass):
    """ Simplify BB graph """


class Pass_SSATranslate(FunctionPass):
    """ Translate basic-block into  Single Static Assignement form """ 
    pass_tag = "ssa_translation"
    def __init__(self, target, description="translate basic-blocks into ssa form pass"):
        FunctionPass.__init__(self, description, target)
        self.memoization_map = {}
        self.top_bb_list = None


    def execute_on_function(self, fct, fct_group):
        """ """
        Log.report(Log.Info, "executing pass {} on fct {}".format(
            self.pass_tag, fct.get_name()))
        optree = fct.get_scheme()
        assert isinstance(optree, BasicBlockList)
        bb_root = optree.get_input(0)
        bbg = BasicBlockGraph(bb_root, optree)
        phi_node_insertion(bbg)
        variable_renaming(bbg)

Log.report(LOG_PASS_INFO, "Registering generate Basic-Blocks pass")
Pass.register(Pass_GenerateBasicBlock)
Log.report(LOG_PASS_INFO, "Registering ssa translation pass")
Pass.register(Pass_SSATranslate)

if __name__ == "__main__":
    bb_root = BasicBlock(tag="bb_root")
    bb_1 = BasicBlock(tag="bb_1")
    bb_2 = BasicBlock(tag="bb_2")
    bb_3 = BasicBlock(tag="bb_3")

    var_x = Variable("x", precision=None)
    var_y = Variable("y", precision=None)

    bb_root.add(ReferenceAssign(var_x, 1))
    bb_root.add(ReferenceAssign(var_y, 2))
    bb_root.add(ConditionalBranch(var_x > var_y, bb_1, bb_2))

    bb_1.add(ReferenceAssign(var_x, 2))
    bb_1.add(UnconditionalBranch(bb_3))

    bb_2.add(ReferenceAssign(var_y, 3))
    bb_2.add(UnconditionalBranch(bb_3))

    bb_3.add(ReferenceAssign(var_y, var_x))


    bb_list = BasicBlockList(tag="main")
    for bb in [bb_root, bb_1, bb_2, bb_3]:
        bb_list.add(bb)

    print(bb_list.get_str(depth=None))

    BBG = BasicBlockGraph(bb_root, bb_list)

    phi_node_insertion(BBG)

    print("after phi node insertion")
    print(bb_list.get_str(depth=None))

    variable_renaming(BBG)
    print("after variable renaming")
    print(bb_list.get_str(depth=None))

