# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import ijson

from pants.backend.go.target_types import GoModSourcesField
from pants.backend.go.util_rules.sdk import GoSdkProcess
from pants.base.specs import AddressSpecs, AscendantAddresses
from pants.build_graph.address import Address
from pants.engine.engine_aware import EngineAwareParameter
from pants.engine.fs import Digest, RemovePrefix
from pants.engine.process import ProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import (
    HydratedSources,
    HydrateSourcesRequest,
    UnexpandedTargets,
    WrappedTarget,
)
from pants.util.ordered_set import FrozenOrderedSet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleDescriptor:
    path: str
    version: str


@dataclass(frozen=True)
class GoModInfo:
    # Import path of the Go module, based on the `module` in `go.mod`.
    import_path: str

    # Modules referenced by this go.mod with resolved versions.
    modules: FrozenOrderedSet[ModuleDescriptor]

    # Digest containing the full paths to `go.mod` and `go.sum`.
    digest: Digest

    # Digest containing `go.mod` and `go.sum` with no path prefixes.
    stripped_digest: Digest


@dataclass(frozen=True)
class GoModInfoRequest(EngineAwareParameter):
    address: Address

    def debug_hint(self) -> str:
        return self.address.spec


def parse_module_descriptors(raw_json: bytes) -> list[ModuleDescriptor]:
    """Parse the JSON output of `go list -m`."""
    if not raw_json:
        return []

    module_descriptors = []
    for raw_module_descriptor in ijson.items(raw_json, "", multiple_values=True):
        if raw_module_descriptor.get("Main", False):
            continue

        module_descriptor = ModuleDescriptor(
            path=raw_module_descriptor["Path"],
            version=raw_module_descriptor["Version"],
        )
        module_descriptors.append(module_descriptor)
    return module_descriptors


@rule
async def determine_go_mod_info(
    request: GoModInfoRequest,
) -> GoModInfo:
    wrapped_target = await Get(WrappedTarget, Address, request.address)
    sources_field = wrapped_target.target[GoModSourcesField]
    go_mod_path = sources_field.go_mod_path
    go_mod_dir = os.path.dirname(go_mod_path)

    # Get the `go.mod` (and `go.sum`) and strip so the file has no directory prefix.
    hydrated_sources = await Get(HydratedSources, HydrateSourcesRequest(sources_field))
    sources_digest = hydrated_sources.snapshot.digest

    mod_json_get = Get(
        ProcessResult,
        GoSdkProcess(
            command=("mod", "edit", "-json"),
            input_digest=sources_digest,
            working_dir=go_mod_dir,
            description=f"Parse {go_mod_path}",
        ),
    )
    list_modules_get = Get(
        ProcessResult,
        GoSdkProcess(
            command=("list", "-m", "-json", "all"),
            input_digest=sources_digest,
            working_dir=go_mod_dir,
            description=f"List modules in {go_mod_path}",
        ),
    )

    stripped_source_get = Get(Digest, RemovePrefix(sources_digest, go_mod_dir))
    mod_json, list_modules, stripped_sources = await MultiGet(
        mod_json_get, list_modules_get, stripped_source_get
    )

    module_metadata = json.loads(mod_json.stdout)
    modules = parse_module_descriptors(list_modules.stdout)
    return GoModInfo(
        import_path=module_metadata["Module"]["Path"],
        modules=FrozenOrderedSet(modules),
        digest=sources_digest,
        stripped_digest=stripped_sources,
    )


# TODO: Have `go_package` have a required field associating with `go_mod` target.
@dataclass(frozen=True)
class OwningGoModRequest:
    address: Address


@dataclass(frozen=True)
class OwningGoMod(EngineAwareParameter):
    address: Address

    def debug_hint(self) -> str:
        return self.address.spec


@rule
async def find_nearest_go_mod(request: OwningGoModRequest) -> OwningGoMod:
    candidate_targets = await Get(
        UnexpandedTargets, AddressSpecs([AscendantAddresses(request.address.spec_path)])
    )
    # Sort by address.spec_path in descending order so the nearest go_mod target is sorted first.
    go_mod_targets = sorted(
        (tgt for tgt in candidate_targets if tgt.has_field(GoModSourcesField)),
        key=lambda tgt: tgt.address.spec_path,
        reverse=True,
    )
    if not go_mod_targets:
        raise Exception(
            f"The target {request.address} does not have any `go_mod` target in any ancestor "
            "BUILD files. To fix, please make sure your project has a `go.mod` file and add a "
            "`go_mod` target (you can run `./pants tailor` to do this)."
        )
    nearest_go_mod_target = go_mod_targets[0]
    return OwningGoMod(nearest_go_mod_target.address)


def rules():
    return collect_rules()