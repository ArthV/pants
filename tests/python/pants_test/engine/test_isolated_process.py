# Copyright 2016 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import os
import unittest
from dataclasses import dataclass
from typing import ClassVar, Tuple

from pants.engine.fs import (
    EMPTY_DIRECTORY_DIGEST,
    Digest,
    FileContent,
    FilesContent,
    InputFilesContent,
    PathGlobs,
    Snapshot,
)
from pants.engine.isolated_process import (
    FallibleProcessResult,
    Process,
    ProcessExecutionFailure,
    ProcessResult,
)
from pants.engine.rules import RootRule, rule
from pants.engine.scheduler import ExecutionError
from pants.engine.selectors import Get
from pants.testutil.test_base import TestBase
from pants.util.contextutil import temporary_dir


@dataclass(frozen=True)
class Concatted:
    value: str


@dataclass(frozen=True)
class BinaryLocation:
    bin_path: str

    def __post_init__(self):
        if not os.path.isfile(self.bin_path) or not os.access(self.bin_path, os.X_OK):
            raise ValueError(f"path {self.bin_path} does not name an existing executable file.")


@dataclass(frozen=True)
class ShellCat:
    """Wrapper class to show an example of using an auxiliary class (which wraps an executable) to
    generate an argv instead of doing it all in CatExecutionRequest.

    This can be used to encapsulate operations such as sanitizing command-line arguments which are
    specific to the executable, which can reduce boilerplate for generating Process instances if the
    executable is used in different ways across multiple different types of process execution
    requests.
    """

    binary_location: BinaryLocation

    @property
    def bin_path(self):
        return self.binary_location.bin_path

    def argv_from_snapshot(self, snapshot):
        cat_file_paths = snapshot.files

        option_like_files = [p for p in cat_file_paths if p.startswith("-")]
        if option_like_files:
            raise ValueError(
                f"invalid file names: '{option_like_files}' look like command-line options"
            )

        # Add /dev/null to the list of files, so that cat doesn't hang forever if no files are in the
        # Snapshot.
        return (self.bin_path, "/dev/null") + tuple(cat_file_paths)


@dataclass(frozen=True)
class CatExecutionRequest:
    shell_cat: ShellCat
    path_globs: PathGlobs


@rule
async def cat_files_process_result_concatted(cat_exe_req: CatExecutionRequest) -> Concatted:
    cat_bin = cat_exe_req.shell_cat
    cat_files_snapshot = await Get[Snapshot](PathGlobs, cat_exe_req.path_globs)
    process = Process(
        argv=cat_bin.argv_from_snapshot(cat_files_snapshot),
        input_files=cat_files_snapshot.directory_digest,
        description="cat some files",
    )
    cat_process_result = await Get[ProcessResult](Process, process)
    return Concatted(cat_process_result.stdout.decode())


def create_cat_stdout_rules():
    return [
        cat_files_process_result_concatted,
        RootRule(CatExecutionRequest),
    ]


@dataclass(frozen=True)
class JavacVersionExecutionRequest:
    binary_location: BinaryLocation
    description: ClassVar[str] = "obtaining javac version"

    @property
    def bin_path(self):
        return self.binary_location.bin_path

    def gen_argv(self):
        return (
            self.bin_path,
            "-version",
        )


@dataclass(frozen=True)
class JavacVersionOutput:
    value: str


@rule
async def get_javac_version_output(
    javac_version_command: JavacVersionExecutionRequest,
) -> JavacVersionOutput:
    javac_version_proc_req = Process(
        argv=javac_version_command.gen_argv(),
        description=javac_version_command.description,
        input_files=EMPTY_DIRECTORY_DIGEST,
    )
    javac_version_proc_result = await Get[ProcessResult](Process, javac_version_proc_req,)

    return JavacVersionOutput(javac_version_proc_result.stderr.decode())


@dataclass(frozen=True)
class JavacSources:
    """Wrapper for the paths to include for Java source files.

    This shows an example of making a custom type to wrap generic types such as str to add usage
    context.

    See CatExecutionRequest and rules above for an example of using PathGlobs
    which does not introduce this additional layer of indirection.
    """

    java_files: Tuple[str, ...]


@dataclass(frozen=True)
class JavacCompileRequest:
    binary_location: BinaryLocation
    javac_sources: JavacSources

    @property
    def bin_path(self):
        return self.binary_location.bin_path

    def argv_from_source_snapshot(self, snapshot):
        return (self.bin_path,) + snapshot.files


@dataclass(frozen=True)
class JavacCompileResult:
    stdout: str
    stderr: str
    directory_digest: Digest


# Note that this rule assumes that no additional classes are generated other than one for each
# source file, i.e. that there are no inner classes, extras generated by annotation processors, etc.
# This rule just serves as documentation for how rules can look - it is not intended to be
# exhaustively correct java compilation.
# This rule/test should be deleted when we have more real java rules (or anything else which serves
# as a suitable rule-writing example).
@rule
async def javac_compile_process_result(
    javac_compile_req: JavacCompileRequest,
) -> JavacCompileResult:
    java_files = javac_compile_req.javac_sources.java_files
    for java_file in java_files:
        if not java_file.endswith(".java"):
            raise ValueError(f"Can only compile .java files but got {java_file}")
    sources_snapshot = await Get[Snapshot](PathGlobs, PathGlobs(java_files))
    output_dirs = tuple({os.path.dirname(java_file) for java_file in java_files})
    process = Process(
        argv=javac_compile_req.argv_from_source_snapshot(sources_snapshot),
        input_files=sources_snapshot.directory_digest,
        output_directories=output_dirs,
        description="javac compilation",
    )
    javac_proc_result = await Get[ProcessResult](Process, process)

    return JavacCompileResult(
        javac_proc_result.stdout.decode(),
        javac_proc_result.stderr.decode(),
        javac_proc_result.output_directory_digest,
    )


def create_javac_compile_rules():
    return [
        javac_compile_process_result,
        RootRule(JavacCompileRequest),
    ]


class ProcessTest(unittest.TestCase):
    def test_create_from_snapshot_with_env(self):
        req = Process(
            argv=("foo",),
            description="Some process",
            env={"VAR": "VAL"},
            input_files=EMPTY_DIRECTORY_DIGEST,
        )
        self.assertEqual(req.env, ("VAR", "VAL"))


class TestInputFileCreation(TestBase):
    def test_input_file_creation(self):
        file_name = "some.filename"
        file_contents = b"some file contents"

        input_file = InputFilesContent((FileContent(path=file_name, content=file_contents),))
        digest = self.request_single_product(Digest, input_file)

        req = Process(
            argv=("/bin/cat", file_name),
            input_files=digest,
            description="cat the contents of this file",
        )

        result = self.request_single_product(ProcessResult, req)
        self.assertEqual(result.stdout, file_contents)

    def test_multiple_file_creation(self):
        input_files_content = InputFilesContent(
            (
                FileContent(path="a.txt", content=b"hello"),
                FileContent(path="b.txt", content=b"goodbye"),
            )
        )

        digest = self.request_single_product(Digest, input_files_content)

        req = Process(
            argv=("/bin/cat", "a.txt", "b.txt"),
            input_files=digest,
            description="cat the contents of this file",
        )

        result = self.request_single_product(ProcessResult, req)
        self.assertEqual(result.stdout, b"hellogoodbye")

    def test_file_in_directory_creation(self):
        path = "somedir/filename"
        content = b"file contents"

        input_file = InputFilesContent((FileContent(path=path, content=content),))
        digest = self.request_single_product(Digest, input_file)

        req = Process(
            argv=("/bin/cat", "somedir/filename"),
            input_files=digest,
            description="Cat a file in a directory to make sure that doesn't break",
        )

        result = self.request_single_product(ProcessResult, req)
        self.assertEqual(result.stdout, content)

    def test_not_executable(self):
        file_name = "echo.sh"
        file_contents = b'#!/bin/bash -eu\necho "Hello"\n'

        input_file = InputFilesContent((FileContent(path=file_name, content=file_contents),))
        digest = self.request_single_product(Digest, input_file)

        req = Process(
            argv=("./echo.sh",), input_files=digest, description="cat the contents of this file",
        )

        with self.assertRaisesWithMessageContaining(ExecutionError, "Permission"):
            self.request_single_product(ProcessResult, req)

    def test_executable(self):
        file_name = "echo.sh"
        file_contents = b'#!/bin/bash -eu\necho "Hello"\n'

        input_file = InputFilesContent(
            (FileContent(path=file_name, content=file_contents, is_executable=True),)
        )
        digest = self.request_single_product(Digest, input_file)

        req = Process(
            argv=("./echo.sh",), input_files=digest, description="cat the contents of this file",
        )

        result = self.request_single_product(ProcessResult, req)
        self.assertEqual(result.stdout, b"Hello\n")


class IsolatedProcessTest(TestBase, unittest.TestCase):
    @classmethod
    def rules(cls):
        return (
            super().rules()
            + [RootRule(JavacVersionExecutionRequest), get_javac_version_output]
            + create_cat_stdout_rules()
            + create_javac_compile_rules()
        )

    def test_integration_concat_with_snapshots_stdout(self):

        self.create_file("f1", "one\n")
        self.create_file("f2", "two\n")

        cat_exe_req = CatExecutionRequest(ShellCat(BinaryLocation("/bin/cat")), PathGlobs(["f*"]),)

        concatted = self.request_single_product(Concatted, cat_exe_req)
        self.assertEqual(Concatted("one\ntwo\n"), concatted)

    def test_javac_version_example(self):
        request = JavacVersionExecutionRequest(BinaryLocation("/usr/bin/javac"))
        result = self.request_single_product(JavacVersionOutput, request)
        self.assertIn("javac", result.value)

    def test_write_file(self):
        request = Process(
            argv=("/bin/bash", "-c", "echo -n 'European Burmese' > roland"),
            description="echo roland",
            output_files=("roland",),
            input_files=EMPTY_DIRECTORY_DIGEST,
        )

        process_result = self.request_single_product(ProcessResult, request)

        self.assertEqual(
            process_result.output_directory_digest,
            Digest(
                fingerprint="63949aa823baf765eff07b946050d76ec0033144c785a94d3ebd82baa931cd16",
                serialized_bytes_length=80,
            ),
        )

        files_content_result = self.request_single_product(
            FilesContent, process_result.output_directory_digest,
        )

        self.assertEqual(
            files_content_result.dependencies, (FileContent("roland", b"European Burmese", False),)
        )

    def test_timeout(self):
        request = Process(
            argv=("/bin/bash", "-c", "/bin/sleep 0.2; /bin/echo -n 'European Burmese'"),
            timeout_seconds=0.1,
            description="sleepy-cat",
            input_files=EMPTY_DIRECTORY_DIGEST,
        )
        result = self.request_single_product(FallibleProcessResult, request)
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(b"Exceeded timeout", result.stdout)
        self.assertIn(b"sleepy-cat", result.stdout)

    def test_javac_compilation_example_success(self):
        self.create_dir("simple")
        self.create_file(
            "simple/Simple.java",
            """package simple;
// Valid java. Totally complies.
class Simple {

}""",
        )

        request = JavacCompileRequest(
            BinaryLocation("/usr/bin/javac"), JavacSources(("simple/Simple.java",)),
        )

        result = self.request_single_product(JavacCompileResult, request)
        files_content = self.request_single_product(
            FilesContent, result.directory_digest
        ).dependencies

        self.assertEqual(
            tuple(sorted(("simple/Simple.java", "simple/Simple.class",))),
            tuple(sorted(file.path for file in files_content)),
        )

        self.assertGreater(len(files_content[0].content), 0)

    def test_javac_compilation_example_failure(self):
        self.create_dir("simple")
        self.create_file(
            "simple/Broken.java",
            """package simple;
class Broken {
  NOT VALID JAVA!
}""",
        )

        request = JavacCompileRequest(
            BinaryLocation("/usr/bin/javac"), JavacSources(("simple/Broken.java",))
        )

        with self.assertRaises(ExecutionError) as cm:
            self.request_single_product(JavacCompileResult, request)
        e = cm.exception.wrapped_exceptions[0]
        self.assertIsInstance(e, ProcessExecutionFailure)
        self.assertEqual(1, e.exit_code)
        self.assertIn("javac compilation", str(e))
        self.assertIn(b"NOT VALID JAVA", e.stderr)

    def test_jdk(self):
        with temporary_dir() as temp_dir:
            with open(os.path.join(temp_dir, "roland"), "w") as f:
                f.write("European Burmese")
            request = Process(
                argv=("/bin/cat", ".jdk/roland"),
                input_files=EMPTY_DIRECTORY_DIGEST,
                description="cat JDK roland",
                jdk_home=temp_dir,
            )
            result = self.request_single_product(ProcessResult, request)
            self.assertEqual(result.stdout, b"European Burmese")

    def test_fallible_failing_command_returns_exited_result(self):
        request = Process(
            argv=("/bin/bash", "-c", "exit 1"),
            description="one-cat",
            input_files=EMPTY_DIRECTORY_DIGEST,
        )

        result = self.request_single_product(FallibleProcessResult, request)

        self.assertEqual(result.exit_code, 1)

    def test_non_fallible_failing_command_raises(self):
        request = Process(
            argv=("/bin/bash", "-c", "exit 1"),
            description="one-cat",
            input_files=EMPTY_DIRECTORY_DIGEST,
        )

        with self.assertRaises(ExecutionError) as cm:
            self.request_single_product(ProcessResult, request)
        self.assertIn("process 'one-cat' failed with exit code 1.", str(cm.exception))