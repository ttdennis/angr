
import logging
from collections import defaultdict

import pyvex

from ....blade import Blade
from ....annocfg import AnnotatedCFG
from .... import sim_options as o
from .... import BP, BP_BEFORE
from ....surveyors import Slicecutor
from .resolver import IndirectJumpResolver


l = logging.getLogger("angr.analyses.cfg.indirect_jump_resolvers.jumptable")


class UninitReadMeta(object):
    uninit_read_base = 0xc000000


class JumpTableResolver(IndirectJumpResolver):
    """
    A generic jump table resolver.

    This is a fast jump table resolution. For performance concerns, we made the following assumptions:
        - The final jump target comes from the memory.
        - The final jump target must be directly read out of the memory, without any further modification or altering.

    """
    def __init__(self, arch, project=None):
        super(JumpTableResolver, self).__init__(arch=arch, timeless=False)

        self._bss_regions = None
        # the maximum number of resolved targets. Will be initialized from CFG.
        self._max_targets = None

        self.project = project

        if self.project is not None:
            self._find_bss_region()

    def filter(self, cfg, addr, func_addr, block, jumpkind):
        # TODO:

        if jumpkind != "Ijk_Boring":
            # Currently we only support boring ones
            return False

        return True

    def resolve(self, cfg, addr, func_addr, block, jumpkind):
        """
        Resolves jump tables.

        :param cfg: A CFG instance.
        :param int addr: IRSB address.
        :param int func_addr: The function address.
        :param pyvex.IRSB block: The IRSB.
        :return: A bool indicating whether the indirect jump is resolved successfully, and a list of resolved targets
        :rtype: tuple
        """

        if jumpkind != 'Ijk_Boring':
            # how did it pass filter()?
            l.error("JumpTableResolver only supports boring jumps.")
            return False, None

        project = cfg.project  # short-hand
        self._max_targets = cfg._indirect_jump_target_limit

        # Perform a backward slicing from the jump target
        b = Blade(cfg.graph, addr, -1, cfg=cfg, project=project, ignore_sp=False, ignore_bp=False, max_level=3)

        stmt_loc = (addr, 'default')
        if stmt_loc not in b.slice:
            return False, None

        load_stmt_loc, load_stmt = None, None
        stmts_to_remove = [stmt_loc]
        while True:
            preds = b.slice.predecessors(stmt_loc)
            if len(preds) != 1:
                return False, None
            block_addr, stmt_idx = stmt_loc = preds[0]
            block = project.factory.block(block_addr).vex
            stmt = block.statements[stmt_idx]
            if isinstance(stmt, (pyvex.IRStmt.WrTmp, pyvex.IRStmt.Put)):
                if isinstance(stmt.data, (pyvex.IRExpr.Get, pyvex.IRExpr.RdTmp)):
                    # data transferring
                    stmts_to_remove.append(stmt_loc)
                    stmt_loc = (block_addr, stmt_idx)
                    continue
                elif isinstance(stmt.data, pyvex.IRExpr.Load):
                    # Got it!
                    stmt_loc = (block_addr, stmt_idx)
                    load_stmt, load_stmt_loc = stmt, stmt_loc
                    stmts_to_remove.append(stmt_loc)
            break

        if load_stmt_loc is None:
            # the load statement is not found
            return False, None

        # skip all statements before the load statement
        b.slice.remove_nodes_from(stmts_to_remove)

        # Debugging output
        if l.level == logging.DEBUG:
            self._dbg_repr_slice(b)

        # Get all sources
        sources = [ n_ for n_ in b.slice.nodes() if b.slice.in_degree(n_) == 0 ]

        # Create the annotated CFG
        annotatedcfg = AnnotatedCFG(project, None, detect_loops=False)
        annotatedcfg.from_digraph(b.slice)

        # pylint: disable=too-many-nested-blocks
        for src_irsb, _ in sources:
            # Use slicecutor to execute each one, and get the address
            # We simply give up if any exception occurs on the way
            start_state = self._initial_state(src_irsb)

            # any read from an uninitialized segment should be unconstrained
            if self._bss_regions:
                bss_memory_read_bp = BP(when=BP_BEFORE, enabled=True, action=self._bss_memory_read_hook)
                start_state.inspect.add_breakpoint('mem_read', bss_memory_read_bp)

            start_state.regs.bp = start_state.arch.initial_sp + 0x2000

            init_registers_on_demand_bp = BP(when=BP_BEFORE, enabled=True, action=self._init_registers_on_demand)
            start_state.inspect.add_breakpoint('mem_read', init_registers_on_demand_bp)

            # Create the slicecutor
            slicecutor = Slicecutor(project, annotatedcfg, start=start_state, targets=(load_stmt_loc[0],),
                                    force_taking_exit=True
                                    )

            # Run it!
            try:
                slicecutor.run()
            except KeyError as ex:
                # This is because the program slice is incomplete.
                # Blade will support more IRExprs and IRStmts
                l.debug("KeyError occurred due to incomplete program slice.", exc_info=ex)
                continue

            # Get the jumping targets
            for r in slicecutor.reached_targets:
                succ = project.factory.successors(r)
                all_states = succ.flat_successors + succ.unconstrained_successors
                if not all_states:
                    l.warning("Slicecutor failed to execute the program slice. No output state is available.")
                    continue

                state = all_states[0]  # Just take the first state

                # Parse the memory load statement
                load_addr_tmp = load_stmt.data.addr.tmp
                if load_addr_tmp not in state.scratch.temps:
                    # the tmp variable is not there... umm...
                    continue
                jump_addr = state.scratch.temps[load_addr_tmp]
                total_cases = jump_addr._model_vsa.cardinality
                all_targets = []

                if total_cases > self._max_targets:
                    # We resolved too many targets for this indirect jump. Something might have gone wrong.
                    l.debug("%d targets are resolved for the indirect jump at %#x. It may not be a jump table",
                            total_cases, addr)
                    return False, None

                    # Or alternatively, we can ask user, which is meh...
                    #
                    # jump_base_addr = int(raw_input("please give me the jump base addr: "), 16)
                    # total_cases = int(raw_input("please give me the total cases: "))
                    # jump_target = state.se.SI(bits=64, lower_bound=jump_base_addr, upper_bound=jump_base_addr +
                    # (total_cases - 1) * 8, stride=8)

                jump_table = []

                for idx, a in enumerate(state.se.any_n_int(jump_addr, total_cases)):
                    if idx % 100 == 0 and idx != 0:
                        l.debug("%d targets have been resolved for the indirect jump at %#x...", idx, addr)
                    jump_target = state.memory.load(a, state.arch.bits / 8, endness=state.arch.memory_endness)
                    target = state.se.any_int(jump_target)
                    all_targets.append(target)
                    jump_table.append(target)

                l.info("Jump table resolution: resolved %d targets from %#x.", len(all_targets), addr)

                # write to the IndirectJump object in CFG
                ij = cfg.indirect_jumps[addr]
                ij.jumptable = True
                ij.jumptable_addr = state.se.min(jump_addr)
                ij.jumptable_targets = jump_table
                ij.jumptable_entries = total_cases

                return True, all_targets

        return False, None

    #
    # Private methods
    #

    def _find_bss_region(self):

        self._bss_regions = [ ]

        # TODO: support other sections other than '.bss'.
        # TODO: this is very hackish. fix it after the chaos.
        for section in self.project.loader.main_bin.sections:
            if section.name == '.bss':
                self._bss_regions.append((section.vaddr, section.memsize))
                break

    def _bss_memory_read_hook(self, state):

        if not self._bss_regions:
            return

        read_addr = state.inspect.mem_read_address
        read_length = state.inspect.mem_read_length

        if not isinstance(read_addr, (int, long)) and read_addr.symbolic:
            # don't touch it
            return

        concrete_read_addr = state.se.any_int(read_addr)
        concrete_read_length = state.se.any_int(read_length)

        for start, size in self._bss_regions:
            if start <= concrete_read_addr < start + size:
                # this is a read from the .bss section
                break
        else:
            return

        if not state.memory.was_written_to(concrete_read_addr):
            # it was never written to before. we overwrite it with unconstrained bytes
            for i in xrange(0, concrete_read_length, self.arch.bits / 8):
                state.memory.store(concrete_read_addr + i, state.se.Unconstrained('unconstrained', self.arch.bits))

                # job done :-)

    @staticmethod
    def _init_registers_on_demand(state):
        # for uninitialized read using a register as the source address, we replace them in memory on demand
        read_addr = state.inspect.mem_read_address

        if not isinstance(read_addr, (int, long)) and read_addr.uninitialized:

            read_length = state.inspect.mem_read_length
            if not isinstance(read_length, (int, long)):
                read_length = read_length._model_vsa.upper_bound
            if read_length > 16:
                return
            new_read_addr = state.se.BVV(UninitReadMeta.uninit_read_base, state.arch.bits)
            UninitReadMeta.uninit_read_base += read_length

            # replace the expression in registers
            state.registers.replace_all(read_addr, new_read_addr)

            state.inspect.mem_read_address = new_read_addr

            # job done :-)

    def _dbg_repr_slice(self, blade):

        stmts = defaultdict(set)

        for addr, stmt_idx in sorted(list(blade.slice.nodes())):
            stmts[addr].add(stmt_idx)

        for addr in sorted(stmts.keys()):
            stmt_ids = stmts[addr]
            irsb = self.project.factory.block(addr).vex

            print "  ####"
            print "  #### Block %#x" % addr
            print "  ####"

            for i, stmt in enumerate(irsb.statements):
                taken = i in stmt_ids
                s = "%s %x:%02d | " % ("+" if taken else " ", addr, i)
                s += "%s " % irsb.statements[i].__str__(arch=self.project.arch, tyenv=irsb.tyenv)
                if taken:
                    s += "IN: %d" % blade.slice.in_degree((addr, i))
                print s

            # the default exit
            default_exit_taken = 'default' in stmt_ids
            s = "%s %x:default | PUT(%s) = %s; %s" % ("+" if default_exit_taken else " ", addr, irsb.offsIP, irsb.next,
                                                      irsb.jumpkind
                                                      )
            print s

    def _initial_state(self, src_irsb):

        state = self.project.factory.blank_state(
            addr=src_irsb,
            mode='static',
            add_options={
                o.DO_RET_EMULATION,
                o.TRUE_RET_EMULATION_GUARD,
                o.AVOID_MULTIVALUED_READS,
            },
            remove_options={
                               o.CGC_ZERO_FILL_UNCONSTRAINED_MEMORY,
                               o.UNINITIALIZED_ACCESS_AWARENESS,
                           } | o.refs
        )

        return state
