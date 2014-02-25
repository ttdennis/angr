#!/usr/bin/env python

import logging
l = logging.getLogger("angr.surveyors.Slicecutor")

from angr import Surveyor, Path

#
# HappyGraph is just here for testing. Please ignore it!
#
from collections import defaultdict
class HappyGraph(object):
	def __init__(self, path = None, paths=None, strict=False):
		if not strict:
			self.jumps = defaultdict(lambda: False)
		else:
			self.jumps = { }

		if paths is None:
			paths = [ ]
		if path is not None:
			paths.append(path)

		for p in paths:
			for i in range(len(p.addr_backtrace) - 1):
				self.jumps[(p.addr_backtrace[i], p.addr_backtrace[i+1])] = True

	def should_take_exit(self, src, dst): # pylint: disable=W0613,R0201,
		return self.jumps[(src, dst)]

	def get_whitelisted_statements(self, addr): # pylint: disable=W0613,R0201,
		return None

	def get_last_statement_index(self, addr): # pylint: disable=W0613,R0201,
		return None

class Slicecutor(Surveyor):
	'''The Slicecutor is a surveyor that executes provided code slices.'''

	def __init__(self, project, annotated_cfg, start=None, starts=None, max_concurrency=None):
		Surveyor.__init__(self, project, start=None, starts=[ ], max_concurrency=max_concurrency)

		# the project we're slicing up!
		self._project = project

		# the annotated cfg to determine what to execute
		self._annotated_cfg = annotated_cfg

		# these are paths that are taking exits that the annotated CFG does not
		# know about
		self.mysteries = [ ]

		# these are paths that we cut due to the slicing
		self.cut = [ ]

		# create the starting paths
		entries = [ ]
		if start is not None: entries.append(start)
		if starts is not None: entries.extend(starts)
		if len(entries) == 0:
			entries.append(project.initial_exit())

		l.debug("%s starting up with %d exits", self, len(entries))
		for e in entries:
			p = self.tick_path_exit(Path(project=project), e)
			print p, p.last_run
			if p is not None:
				self.active.append(p)
		l.debug("... which created %d paths", len(self.active))

	def tick_path_exit(self, p, e):
		addr = e.concretize()
		whitelist = self._annotated_cfg.get_whitelisted_statements(addr)
		last_stmt = self._annotated_cfg.get_last_statement_index(addr)
		return p.continue_through_exit(e, stmt_whitelist=whitelist, last_stmt=last_stmt)

	def tick_path(self, path):
		path_exits = path.flat_exits(reachable=True)
		new_paths = [ ]

		mystery = False
		cut = False

		l.debug("%s ticking path %s", self, path)

		for e in path_exits:
			dst_addr = e.concretize()
			l.debug("... checking exit to 0x%x from %s", dst_addr, path.last_run)
			try:
				taken = self._annotated_cfg.should_take_exit(path.last_run.addr, dst_addr)
			except Exception: # TODO: which exception?
				l.debug("... annotated CFG did not know about it!")
				mystery = True
				continue

			if taken:
				l.debug("... taking the exit.")
				p = self.tick_path_exit(path, e)
				if p: new_paths.append(p)
				# the else case isn't here, because the path should set errored in this
				# case and we'll catch it below
			else:
				l.debug("... not taking the exit.")
				cut = True

		if mystery: self.mysteries.append(path)
		if cut: self.cut.append(path)
		return new_paths

	def __str__(self):
		return "<Slicecutor with paths: %s, %d cut, %d mysteries>" % (Surveyor.__str__(self), len(self.cut), len(self.mysteries))