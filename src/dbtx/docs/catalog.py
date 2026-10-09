"""Docs catalog module."""

from enum import Enum
from typing import Any, List, Dict, Optional
import json

import yaml

from dbtx.docs.exposure_reports import ReportAsset

DEFAULT_HISTORY_LIMIT: int = 3
# The `--vars` key naming the job an invocation belongs to. run_results.json is
# the only dbt artifact that records an invocation's vars at all -- manifest.json
# carries no args -- so it is the sole place a job id can be recovered from.
DEFAULT_JOB_ID_VAR: str = "job_id"


class NodeTypes(str, Enum):
    MODEL = "model"
    SNAPSHOT = "snapshot"
    SOURCE = "source"
    EXPOSURE = "exposure"
    TEST = "test"

DBT_MANIFEST_CHECK_KEYS: List[str] = [
    'metadata',
    'nodes',
    'sources',
    'macros',
    'docs',
    'exposures',
    'groups',
    'selectors',
    'disabled',
    'parent_map',
    'child_map',
    'group_map',
    'unit_tests',
    'functions',
]

class NodeResult:
    """Repreents a documentation nodes last result set"""

    def __init__(self, node_name: str, node_status: str, execution_time: float,
                 compile_time: Optional[str], run_time: Optional[str],
                 checksum: Optional[str] = None,
                 run_invocation_id: Optional[str] = None,
                 run_generated_at: Optional[str] = None,
                 job_id: Optional[str] = None,
                 stale: bool = False):
        self.node_name: str = node_name
        self.node_status: str = node_status
        self.execution_time: float = execution_time
        self.last_compiled_at: Optional[str] = compile_time
        self.last_ran_at: Optional[str] = run_time
        # `checksum` is the node checksum as of the run that produced this
        # result -- the version of the SQL that actually executed. It is
        # compared against the docs manifest to derive `stale`.
        self.checksum: Optional[str] = checksum
        self.run_invocation_id: Optional[str] = run_invocation_id
        self.run_generated_at: Optional[str] = run_generated_at
        # The job this run belonged to, read from the invocation's `--vars`.
        # None both for invocations that passed no such var and for results
        # recorded before job ids were tracked.
        self.job_id: Optional[str] = job_id
        self.stale: bool = stale

    def to_dict(self) -> dict:
        return {
            'status': self.node_status,
            'execution_time': self.execution_time,
            'last_compiled_at': self.last_compiled_at,
            'last_ran_at': self.last_ran_at,
            'checksum': self.checksum,
            'run_invocation_id': self.run_invocation_id,
            'run_generated_at': self.run_generated_at,
            'job_id': self.job_id,
            'stale': self.stale,
        }

    @classmethod
    def from_dict(cls, node_name: str, dat: dict) -> "NodeResult":
        return cls(
            node_name=node_name,
            node_status=dat['status'],
            execution_time=dat['execution_time'],
            compile_time=dat.get('last_compiled_at'),
            run_time=dat.get('last_ran_at'),
            checksum=dat.get('checksum'),
            run_invocation_id=dat.get('run_invocation_id'),
            run_generated_at=dat.get('run_generated_at'),
            job_id=dat.get('job_id'),
            stale=dat.get('stale', False),
        )

    def __str__(self):
        return f"'{self.node_name}' | '{self.node_status}'"

    def __repr__(self):
        return f"RunResultNode(name='{self.node_name}', run_status='{self.node_status}')"


class DocNode:
    """Represents a documentation node."""

    def __init__(self, name: str, checksum: str, dat: dict,):
        self.name = name
        self.checksum = checksum
        self.json_dat: dict = dat
        # Newest result first, capped at the catalog's history limit.
        self.run_history: List[NodeResult] = []
        # The rendered report belonging to this node, for exposures that have
        # one. Discovered from disk on each patch rather than accumulated, so it
        # is never hydrated from the sidecar.
        self.report: Optional[ReportAsset] = None

    @property
    def run_result(self) -> Optional[NodeResult]:
        """The most recent run result, or None if this node has never run."""
        return self.run_history[0] if self.run_history else None

    def setRunResult(self, run_result: NodeResult, history_limit: int = DEFAULT_HISTORY_LIMIT,):
        """Records `run_result` as the newest run, keeping up to `history_limit` runs.

        Re-recording the same invocation replaces the head rather than
        appending, so patching the same run twice is a no-op.
        """
        head = self.run_result
        same_invocation = (
            head is not None
            and run_result.run_invocation_id is not None
            and head.run_invocation_id == run_result.run_invocation_id
        )
        if same_invocation:
            self.run_history[0] = run_result
        else:
            self.run_history.insert(0, run_result)
        del self.run_history[max(history_limit, 1):]

    def is_newer_than_head(self, run_result: NodeResult) -> bool:
        """Whether `run_result` post-dates the currently recorded newest run.

        Guards against a late patch of older artifacts rolling a node back.
        Results carrying no timestamp, or belonging to the invocation already
        at the head, are treated as applicable.
        """
        head = self.run_result
        if head is None:
            return True
        if run_result.run_invocation_id is not None and head.run_invocation_id == run_result.run_invocation_id:
            return True
        if run_result.run_generated_at is None or head.run_generated_at is None:
            return True
        return run_result.run_generated_at > head.run_generated_at

    def __str__(self):
        return f"'{self.name}' @ '{self.checksum}'"

    def __repr__(self):
        return f"DocNode(name='{self.name}', checksum='{self.checksum}')"


class PatchTally:
    """Counts of what a run overlay did, for reporting back to the caller."""

    def __init__(self):
        self.patched: List[str] = []
        self.ignored: List[str] = []
        self.superseded: List[str] = []

    def __str__(self):
        parts = [f"patched {len(self.patched)}"]
        if self.ignored:
            parts.append(f"ignored {len(self.ignored)} (not in docs)")
        if self.superseded:
            parts.append(f"skipped {len(self.superseded)} (older than recorded run)")
        return ", ".join(parts)

    def __repr__(self):
        return (f"PatchTally(patched={len(self.patched)}, ignored={len(self.ignored)}, "
                f"superseded={len(self.superseded)})")


def read_job_id(rr_data: dict, job_id_var: str = DEFAULT_JOB_ID_VAR) -> Optional[str]:
    """The job id an invocation was launched with, read from its run results.

    dbt copies the resolved command line into run_results' top-level `args`
    verbatim, whether or not the project reads the var, which is what makes a
    job id recoverable after the fact. `metadata.env` is consulted as a fallback
    so projects that tag invocations with `DBT_ENV_CUSTOM_ENV_<var>` instead of
    `--vars` land in the same field.
    """
    raw: Any = (rr_data.get('args') or {}).get('vars')
    if isinstance(raw, str):
        # Some dbt versions, and CI wrappers that rebuild the artifact, leave
        # `vars` as the unparsed YAML string it arrived as.
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError:
            raw = None

    for source in (raw, (rr_data.get('metadata') or {}).get('env')):
        if isinstance(source, dict):
            value = source.get(job_id_var)
            if value is not None and value != "":
                return str(value)
    return None


class DocsCatalog:
    """Parser for documentation files."""

    def __init__(self, history_limit: int = DEFAULT_HISTORY_LIMIT):
        self.history_limit: int = max(history_limit, 1)
        self.metadata: dict = {}
        self.nodes: Dict[str, DocNode] = {}
        self.parsed: bool = False
        # Job id of the run most recently passed to `parse_run`, so callers can
        # report what they just recorded.
        self.run_job_id: Optional[str] = None

    def _determine_node_is_test(self, node_name: str) -> bool:
        if 'test' in node_name[:4]:
            return True
        return False

    def _node_checksum(self, node_name: str, node_dat: dict) -> str:
        """The node's content checksum, falling back to its name when absent.

        dbt has historically emitted a 'none' checksum for generated nodes such
        as schema tests; a real checksum is preferred wherever present, since it
        is what makes staleness detectable.
        """
        checksum = (node_dat.get('checksum') or {}).get('checksum')
        if checksum:
            return checksum
        return node_name

    def _parse_manifest_nodes(self, nodes: dict,) -> Dict[str, DocNode]:
        doc_nodes: Dict[str, DocNode] = {}

        for node_name, node_dat in nodes.items():
            doc_nodes[node_name] = DocNode(node_name, self._node_checksum(node_name, node_dat), node_dat)

        return doc_nodes

    def parse_manifest(self, manifest_file: str,):
        """Parses a manifest compatible file, establishing the authoritative node set."""

        # Load serialized JSON data from manifest compatible file
        sjson = ""
        with open(manifest_file, "r") as file:
            sjson = file.read()
        manifest_dat = json.loads(sjson)

        # Load each relevant json section
        self.metadata = manifest_dat['metadata']
        self.nodes = self._parse_manifest_nodes(manifest_dat['nodes'])
        # Exposures sit in their own manifest section rather than under `nodes`,
        # but the docs site routes to them identically and run_results reports
        # them like any other node, so they belong in the same keyspace here.
        # Without this they were reported as `ignored (not in docs)` on patch.
        self.nodes.update(self._parse_manifest_nodes(manifest_dat.get('exposures') or {}))
        self.parsed = True

    def _parse_runresults(self, results: dict, checksums: Optional[Dict[str, str]] = None,
                          invocation_id: Optional[str] = None,
                          generated_at: Optional[str] = None,
                          job_id: Optional[str] = None,) -> Dict[str, NodeResult]:
        run_results: Dict[str, NodeResult] = {}
        checksums = checksums or {}

        for result in results:
            node_name: str = result['unique_id']
            node_status: str = result['status']
            node_execution_time: float = result['execution_time']
            # Timing entries are keyed by phase name rather than position: a
            # skipped node carries an empty `timing` array, and the ordering of
            # the phases is not guaranteed by the run-results schema.
            timings: Dict[str, str] = {t['name']: t['completed_at'] for t in result['timing']}
            node_compiled_at: Optional[str] = timings.get('compile')
            node_executed_at: Optional[str] = timings.get('execute')

            node_result = NodeResult(
                node_name=node_name, node_status=node_status, execution_time=node_execution_time,
                compile_time=node_compiled_at, run_time=node_executed_at,
                checksum=checksums.get(node_name),
                run_invocation_id=invocation_id,
                run_generated_at=generated_at,
                job_id=job_id)
            run_results[node_name] = node_result

        return run_results

    def _read_run_checksums(self, run_manifest_file: str) -> Dict[str, str]:
        """Node checksums as of the run, read from the run target's own manifest."""
        with open(run_manifest_file, "r") as file:
            manifest_dat = json.loads(file.read())
        return {
            name: self._node_checksum(name, dat)
            for name, dat in manifest_dat['nodes'].items()
        }

    def parse_run(self, runresults_file: str, run_manifest_file: Optional[str] = None,
                  force: bool = False,
                  job_id_var: str = DEFAULT_JOB_ID_VAR,) -> PatchTally:
        """Overlays run-result data onto the nodes loaded from the docs manifest.

        Only nodes present in the docs manifest are touched; results for
        anything else are ignored and reported in the returned tally. Nodes
        absent from this run keep whatever history they already carry, which is
        what lets a sequence of partial runs accumulate.

        `run_manifest_file` supplies the checksums of the SQL that actually ran
        and is what makes staleness detectable; without it results are recorded
        with no checksum and are never marked stale.

        `job_id_var` names the `--vars` key to record as each result's job id.
        """

        # Load serialized results data from run-results compatible file
        sjson = ""
        with open(runresults_file, "r") as file:
            sjson = file.read()
        rr_data = json.loads(sjson)
        rr_metadata = rr_data.get('metadata', {})

        checksums = self._read_run_checksums(run_manifest_file) if run_manifest_file else None

        # Parse each node for run results
        self.run_job_id = read_job_id(rr_data, job_id_var)

        run_results = self._parse_runresults(
            results=rr_data['results'],
            checksums=checksums,
            invocation_id=rr_metadata.get('invocation_id'),
            generated_at=rr_metadata.get('generated_at'),
            job_id=self.run_job_id)

        # Set the run result for each node that the docs actually describe
        tally = PatchTally()
        for result_node, node_result in run_results.items():
            doc_node = self.nodes.get(result_node)
            if doc_node is None:
                # TODO: add nodes that exist in the run target but not the docs.
                tally.ignored.append(result_node)
                continue
            if not force and not doc_node.is_newer_than_head(node_result):
                tally.superseded.append(result_node)
                continue
            doc_node.setRunResult(node_result, history_limit=self.history_limit)
            tally.patched.append(result_node)

        self.recompute_staleness()
        return tally

    def recompute_staleness(self):
        """Flags recorded results whose SQL no longer matches the docs manifest.

        Run across every node on each patch, not just the ones a run touched: a
        regenerated docs manifest can stale out results no run has revisited.
        """
        for node in self.nodes.values():
            for result in node.run_history:
                result.stale = result.checksum is not None and result.checksum != node.checksum
