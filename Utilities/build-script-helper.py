#!/usr/bin/env python

"""
 This source file is part of the Swift.org open source project

 Copyright (c) 2014 - 2019 Apple Inc. and the Swift project authors
 Licensed under Apache License v2.0 with Runtime Library Exception

 See http://swift.org/LICENSE.txt for license information
 See http://swift.org/CONTRIBUTORS.txt for Swift project authors

 -------------------------------------------------------------------------
"""

from __future__ import print_function

try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO
import argparse
import codecs
import copy
import distutils.dir_util
import distutils.file_util
import errno
import json
import multiprocessing
import os
import pipes
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

g_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
g_source_root = os.path.join(g_project_root, "Sources")

if platform.system() == 'Darwin':
    g_default_sysroot = subprocess.check_output(
        ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
        universal_newlines=True).strip()



# -----------------------------------------------------------
# main
# -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="""
        This script helps in building tools-support-core under different configurations for CI.
        """)
    subparsers = parser.add_subparsers(dest='command')

    # clean
    parser_clean = subparsers.add_parser("clean", help="cleans build artifacts")
    parser_clean.set_defaults(func=clean)
    add_global_args(parser_clean)

    # build
    parser_build = subparsers.add_parser("build", help="builds tools-support-core using CMake")
    parser_build.set_defaults(func=build)
    add_build_args(parser_build)

    # test
    parser_test = subparsers.add_parser("test", help="builds and tests tools-support-core")
    parser_test.set_defaults(func=test)
    add_build_args(parser_test)

    args = parser.parse_args()
    args.func = args.func or build
    args.func(args)

# -----------------------------------------------------------
# Argument parsing
# -----------------------------------------------------------

def add_global_args(parser):
    parser.add_argument(
        "--build-dir",
        dest="build_dir",
        help="path where products will be built [%(default)s]",
        default=".build",
        metavar="PATH")
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="whether to print verbose output")

def add_build_args(parser):
    add_global_args(parser)
    parser.add_argument(
        "--swiftc",
        dest="swiftc_path",
        help="path to the swift compiler [%(default)s]",
        metavar="PATH")
    parser.add_argument(
        "--release",
        action="store_true",
        help="enables building SwiftPM in release mode")

def add_test_args(parser):
    add_build_args(parser)
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="whether to ryb tests in parallel",
        default=True)
    parser.add_argument(
        "--filter",
        action="append",
        help="filter to apply on which tests to run",
        default=[])

def clean_global_args(args):
    args.build_dir = os.path.abspath(args.build_dir)

def clean_build_args(args):
    clean_global_args(args)

    if args.swiftc_path:
        args.swiftc_path = os.path.abspath(args.swiftc_path)

    args.swiftc_path = args.swiftc_path or get_swiftc_path(args)
    args.target_dir = os.path.join(args.build_dir, get_build_target(args))
    args.conf = 'release' if args.release else 'debug'
    args.bin_dir = os.path.join(args.target_dir, args.conf)

def clean_test_args(args):
    clean_build_args(args)

# -----------------------------------------------------------
# Actions
# -----------------------------------------------------------

def clean(args):
    """Cleans the build artifacts."""
    note("Cleaning")
    clean_global_args(args)

    call(args, ["rm", "-rf", args.build_dir])

def build(args):
    """Builds SwiftPM using a two-step process: first using CMake, then with itself."""
    clean_build_args(args)

    # Build llbuild if its build path is not passed in.
    if not args.llbuild_build_dir:
        build_llbuild(args)

    build_swiftpm_with_cmake(args)
    build_swiftpm_with_swiftpm(args)

def test(args):
    """Builds SwiftPM, then tests itself."""
    build(args)

    note("Testing")
    clean_test_args(args)
    cmd = [os.path.join(args.bin_dir, "swift-test")]
    if args.parallel:
        cmd.append("--parallel")
    for arg in args.filter:
        cmd.extend(["--filter", arg])
    call_swiftpm(args, cmd)

def install(args):
    """Builds SwiftPM, then installs its build products."""
    build(args)

    note("Installing")
    call(args, ["ninja", "install"], cwd=args.bootstrap_dir)

# -----------------------------------------------------------
# Build-related helper functionss
# -----------------------------------------------------------

def get_swiftc_path(args):
    try:
        if os.getenv("SWIFT_EXEC"):
            swiftc_path = os.path.realpath(os.getenv("SWIFT_EXEC"))
            if os.path.basename(swiftc_path) == 'swift':
                swiftc_path = swiftc_path + 'c'
            return swiftc_path
        elif platform.system() == 'Darwin':
            return call_output(args, [
                "xcrun", "--find", "swiftc"
            ], stderr=subprocess.PIPE, universal_newlines=True).strip()
        else:
            return call_output(args, ["which", "swiftc"], universal_newlines=True).strip()
    except:
        error("unable to find 'swiftc' tool for bootstrap build")

def get_llbuild_cmake_arg(args):
    if args.llbuild_link_framework:
        return "-DLLBUILD_FRAMEWORK=%s" % args.llbuild_build_dir
    else:
        llbuild_dir = os.path.join(args.llbuild_build_dir, "cmake/modules")
        return "-DLLBuild_DIR=" + llbuild_dir

def get_llbuild_source_path(args):
    llbuild_path = os.path.join(g_project_root, "..", "llbuild")
    if os.path.exists(llbuild_path):
        return llbuild_path
    note("clone llbuild next to swiftpm directory; see development docs: https://github.com/apple/swift-package-manager/blob/master/Documentation/Development.md#using-trunk-snapshot")
    error("unable to find llbuild source directory at %s" % llbuild_path)

def get_build_target(args):
    if platform.system() == 'Darwin':
        return "x86_64-apple-macosx"
    else:
        return call_output(args, ["clang", "--print-target-triple"], universal_newlines=True).strip()

def get_swiftpm_env_cmd(args):
    env_cmd = [
        "env",
        "SWIFT_EXEC=" + args.swiftc_path,
        "SWIFTPM_BUILD_DIR=" + args.build_dir,
        "SWIFTPM_PD_LIBS=" + os.path.join(args.bootstrap_dir, "pm"),
    ]

    if args.llbuild_link_framework:
        env_cmd.append("DYLD_FRAMEWORK_PATH=%s" % args.llbuild_build_dir)
        # FIXME: We always need to pass this.
        env_cmd.append("SWIFTPM_BOOTSTRAP=1")
    else:
        env_cmd.append("SWIFTCI_USE_LOCAL_DEPS=1")

    libs_joined = ":".join([
        os.path.join(args.bootstrap_dir, "lib"),
        os.path.join(args.llbuild_build_dir, "lib"),
    ])

    env_cmd.append("DYLD_LIBRARY_PATH=%s" % libs_joined)
    env_cmd.append("LD_LIBRARY_PATH=%s" % libs_joined)

    return env_cmd

def get_swiftpm_flags(args):
    build_flags = [
        # No need for indexing while building.
        "--disable-index-store",
    ]

    if args.release:
        build_flags.extend([
            "-Xswiftc", "-enable-testing",
            "--configuration", "release"
        ])

    return build_flags

# -----------------------------------------------------------
# Build functions
# -----------------------------------------------------------

def make_symlinks(args):
    """Make symlinks so runtimes can be found automatically when running the inferior."""
    runtimes_dir = os.path.join(args.target_dir, "lib/swift")
    mkdir_p(runtimes_dir)
    symlink_force(os.path.join(args.bootstrap_dir, "pm"), runtimes_dir)

def build_with_cmake(args, cmake_args, source_path, build_dir):
    # Run CMake if needed.
    cache_path = os.path.join(build_dir, "CMakeCache.txt")
    if not os.path.isfile(cache_path):
        swift_flags = ""
        if platform.system() == 'Darwin':
            swift_flags = "-sdk " + g_default_sysroot

        cmd = [
            "cmake", "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE:=Debug",
            "-DCMAKE_Swift_FLAGS=" + swift_flags,
            "-DCMAKE_Swift_COMPILER:=%s" % (args.swiftc_path),
        ] + cmake_args + [source_path]

        if args.verbose:
            print(' '.join(cmd))

        mkdir_p(build_dir)
        call(args, cmd, cwd=build_dir)

    # Build.
    ninja_cmd = ["ninja"]

    if args.verbose:
        ninja_cmd.append("-v")

    call(args, ninja_cmd, cwd=build_dir)

def build_llbuild(args):
    note("Building llbuild")

    # Set where we are going to build llbuild for future steps to find it
    args.llbuild_build_dir = os.path.join(args.target_dir, "llbuild")

    api_dir = os.path.join(args.llbuild_build_dir, ".cmake/api/v1/query")
    mkdir_p(api_dir)
    call(args, ["touch", "codemodel-v2"], cwd=api_dir)

    llbuild_source_dir = args.llbuild_source_dir or get_llbuild_source_path(args)
    build_with_cmake(args, [
        "-DCMAKE_C_COMPILER:=clang",
        "-DCMAKE_CXX_COMPILER:=clang++",
        "-DLLBUILD_SUPPORT_BINDINGS:=Swift",
        "-DSQLite3_INCLUDE_DIR=%s/usr/include" % (g_default_sysroot),
    ], llbuild_source_dir, args.llbuild_build_dir)

def build_swiftpm_with_cmake(args):
    note("Building SwiftPM (with CMake)")

    build_with_cmake(args, [
        get_llbuild_cmake_arg(args),
        "-DSWIFTPM_BUILD_DIR=" + args.bin_dir,
        "-DUSE_VENDORED_TSC=ON",
        "-DCMAKE_INSTALL_PREFIX=" + args.install_prefixes[0],
        "-DINSTALL_LIBSWIFTPM=" + ("ON" if args.install_libspm else "OFF"),
    ], g_project_root, args.bootstrap_dir)

    make_symlinks(args)

def call_swiftpm(args, cmd):
    full_cmd = get_swiftpm_env_cmd(args) + cmd + get_swiftpm_flags(args)
    call(args, full_cmd, cwd=g_project_root)

def build_swiftpm_with_swiftpm(args):
    note("Building SwiftPM (with swift-build)")
    call_swiftpm(args, [
        os.path.join(args.bootstrap_dir, "bin/swift-build"),
        # Always build tests in stage2.
        "--build-tests"
    ])

# -----------------------------------------------------------
# Shell helper functions
# -----------------------------------------------------------

def note(message):
    print("--- %s: note: %s" % (os.path.basename(sys.argv[0]), message))
    sys.stdout.flush()

def error(message):
    print("--- %s: error: %s" % (os.path.basename(sys.argv[0]), message))
    sys.stdout.flush()
    raise SystemExit(1)

def symlink_force(target, link_name):
    if os.path.isdir(link_name):
        link_name = os.path.join(link_name, os.path.basename(target))
    try:
        os.symlink(target, link_name)
    except OSError as e:
        if e.errno == errno.EEXIST:
            os.remove(link_name)
            os.symlink(target, link_name)
        else:
            raise e

def mkdir_p(path):
    """Create the given directory, if it does not exist."""
    try:
        os.makedirs(path)
    except OSError as e:
        # Ignore EEXIST, which may occur during a race condition.
        if e.errno != errno.EEXIST:
            raise

def call(args, cmd, cwd=None):
    if args.verbose:
        print(' '.join(cmd))
    try:
        result = subprocess.check_call(cmd, cwd=cwd)
        if result != 0:
            raise Exception()
    except:
        if not args.verbose:
            print(' '.join(cmd))
        error("command failed with exit status %d" % (result,))

def call_output(args, cmd, cwd=None, stderr=False, universal_newlines=False):
    if args.verbose:
        print(' '.join(cmd))
    return subprocess.check_output(cmd, cwd=cwd, stderr=stderr, universal_newlines=universal_newlines)

if __name__ == '__main__':
    main()
