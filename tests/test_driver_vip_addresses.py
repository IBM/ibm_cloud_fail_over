"""Driver for exercising vip_addresses handling in ibm_cloud_fail_over.py.

No Pacemaker, no IBM Cloud and no root are needed in the default (mock) mode:
  * subprocess.run is replaced with a recorder, so the exact 'ip a add/del ...'
    commands the library would execute are captured and asserted on.
  * the IBM Cloud VPC API and the instance metadata service are faked.

Usage:
  python3 test_driver_vip_addresses.py                    # mock mode (any OS)
  sudo python3 test_driver_vip_addresses.py --live IFACE VIP[,VIP...]
      # Linux only: really runs 'ip a add/del' on IFACE and verifies it,
      # e.g.  --live lo 192.0.2.10,192.0.2.11   (192.0.2.0/24 is TEST-NET-1)
      # An entry may carry its own prefix, e.g. 192.0.2.12/24.

This file must stay valid on Python 3.9: no 'X | Y' type unions, no 'match'.
"""
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

# ---------------------------------------------------------------------------
# Use the installed ibm_cloud_fail_over package if there is one; otherwise fall
# back to the repo's src/ layout.  append() (not insert) keeps an installed
# copy ahead of the checkout on sys.path.
# ---------------------------------------------------------------------------
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

# ---------------------------------------------------------------------------
# ibm_cloud_fail_over.py has these two lines near the top:
#
#   from dotenv import load_dotenv
#   from ibm_cloud_sdk_core import ApiException
#
# These are third-party packages that must be pip-installed in production.
# On a developer laptop or CI runner that does not have them, the import of
# the library itself would fail before a single test ran.
#
# The try/except blocks below solve that: they run BEFORE we import the
# library.  Python's import machinery caches every imported module in the
# global dictionary sys.modules.  If we insert a fake module object there
# under the exact name the library will ask for ("dotenv",
# "ibm_cloud_sdk_core"), Python finds it in the cache and never goes looking
# for the real package on disk.
#
# ibm_cloud_sdk_core stub ---------------------------------------------------
#   The library only uses ApiException from this package.  We define our own
#   ApiException class (it just needs to be an Exception subclass so that
#   'raise ApiException(...)' and 'except ApiException' both work), attach
#   it to a fake module object, and register it.  Any code in the library
#   that does 'from ibm_cloud_sdk_core import ApiException' will then get
#   our version instead.
# ---------------------------------------------------------------------------
try:
    import ibm_cloud_sdk_core  # noqa: F401
except ImportError:
    _m = types.ModuleType("ibm_cloud_sdk_core")

    class ApiException(Exception):
        """Minimal stand-in for ibm_cloud_sdk_core.ApiException."""
        pass

    _m.ApiException = ApiException
    sys.modules["ibm_cloud_sdk_core"] = _m

# Now it is safe to import the library – all its dependencies are satisfied.
# Import the submodule (not the package) because the tests patch names that
# live in it (run, v, ApiException) and the package __init__ does not export them.
from ibm_cloud_fail_over import ibm_cloud_fail_over as lib  # noqa: E402

# ---------------------------------------------------------------------------
# Test constants – match the two-node cluster described in the plugin docs.
# ---------------------------------------------------------------------------
EXT_IP_1 = "10.240.0.4"   # permanent private IP of Node A
EXT_IP_2 = "10.240.0.5"   # permanent private IP of Node B
IFACE    = "eth0"          # local network interface name
VIPS     = ["192.168.100.1", "192.168.100.2"]   # virtual IPs to assign


def prefix(vip):
    """Return the address in the form the library hands to 'ip'.

    _update_os_iface_vip_addresses appends /32 to a bare IP and leaves an entry that
    already contains a '/' alone.  The tests need the same rule in order to
    build the command they expect, and to key the failure overrides in
    fake_run (which sees the prefixed form, not the bare IP).
    """
    return vip if "/" in vip else vip + "/32"


# ---------------------------------------------------------------------------
# FakeCloud – stand-in for the IBM Cloud VPC API
#
# The library calls self._make_api_request(method, path, ...) whenever it
# needs to talk to IBM Cloud.  FakeCloud.request() has the same signature and
# returns just enough data to let the library code proceed:
#
#   GET /routing_tables  → one table called "tbl"
#   GET /routes          → one route with next_hop == EXT_IP_1 in zone us-south-1
#   PATCH / POST / DELETE → empty dict (success, nothing to parse)
#
# self.calls records every (method, path) so tests can verify that a PATCH
# did or did not happen.
#
# self.fail_methods lets a test make a whole HTTP method fail (for example
# {"PATCH"}), which is how we simulate "the cloud rejected the route update"
# without needing a real error response.
# ---------------------------------------------------------------------------
class FakeCloud:
    """Fake VPC API: one routing table, one route whose next hop is ext_ip_1."""

    def __init__(self):
        self.calls = []            # list of (method, path) tuples
        self.fail_methods = set()  # HTTP methods that should raise ApiException

    def request(self, method, path, headers=None, body=None):
        self.calls.append((method, path))
        if method in self.fail_methods:
            # Same exception type the real _make_api_request raises for a
            # non-2xx response, so the library's 'except ApiException' paths
            # are exercised exactly as in production.
            raise lib.ApiException("simulated {} failure".format(method))
        if method == "GET" and "/routes?" in path:
            return {"routes": [{
                "id": "r1", "name": "vip-route",
                "destination": "192.168.100.0/24",
                "zone": {"name": "us-south-1"},
                "advertise": True, "priority": 2,
                "next_hop": {"address": EXT_IP_1},
            }]}
        if method == "GET" and "/routing_tables?" in path:
            return {"routing_tables": [{"id": "rt1", "name": "tbl"}]}
        return {}   # PATCH, POST, DELETE – success with no body

    def patches(self):
        """Return only the PATCH calls recorded so far."""
        return [c for c in self.calls if c[0] == "PATCH"]


# ---------------------------------------------------------------------------
# Base test class – sets up all mocks that every test needs
# ---------------------------------------------------------------------------
class Base(unittest.TestCase):

    def setUp(self):
        # Collects the exact command lists that fake_run receives.
        self.ip_cmds = []

        # Per-VIP return-code overrides, keyed by the PREFIXED address that
        # appears in the command (for example "192.168.100.1/32").  Leave
        # empty for the happy path; set ip_rc[prefix(vip)] = non-zero to
        # simulate 'ip a add/del' exiting with that code for that address.
        self.ip_rc = {}

        # -------------------------------------------------------------------
        # fake_run – replaces the library's 'run' function for every call it
        # makes.
        #
        # The command the library builds is:
        #   ["ip", "a", "add", "192.168.100.1/32", "dev", "eth0"]
        #      0     1     2            3             4      5
        #
        # so cmd[3] is the prefixed address and cmd[2] is the operation
        # ("add" for SET, "del" for UNSET).
        #
        # The library now calls run(..., check=False) and inspects
        # result.returncode itself, instead of relying on subprocess to raise.
        # fake_run therefore just returns a CompletedProcess carrying the
        # return code.  The check=True branch is kept so the fake still
        # behaves like the real subprocess.run if a future library change
        # goes back to check=True.
        # -------------------------------------------------------------------
        def fake_run(cmd, *a, **k):
            self.ip_cmds.append(list(cmd))            # record the command
            rc = self.ip_rc.get(cmd[3], 0)            # look up failure override
            if rc and k.get("check"):                 # non-zero rc + check=True?
                raise subprocess.CalledProcessError(rc, cmd)
            return subprocess.CompletedProcess(cmd, rc, "", "")

        self.cloud = FakeCloud()

        # -------------------------------------------------------------------
        # mock.patch.object(target, attribute, ...) temporarily replaces
        # target.attribute with a fake for the duration of the test.
        #
        # Patch 1 – lib.run → fake_run
        # ----------------------------
        # The library file now has:
        #     from subprocess import CalledProcessError, run
        # That statement copies the function into the library module's own
        # namespace under the name 'run'.  The library has NO attribute called
        # 'subprocess' any more, so the old target (lib.subprocess) would fail
        # with AttributeError.  lib.run IS the name the library looks up each
        # time it executes 'run(...)', so replacing it there intercepts every
        # call.  This is the "patch where it is used, not where it is defined"
        # rule: patching subprocess.run itself would not affect the library,
        # because it already holds its own reference to the original function.
        #
        # Patch 2 – HAFailOver._make_api_request → FakeCloud.request
        # ------------------------------------------------------------
        # _make_api_request is an instance method on the HAFailOver class.
        # We patch it on the CLASS (lib.HAFailOver), not on any particular
        # instance, which means every instance created during the test will
        # have the fake version.  side_effect=lambda *a, **k: ... lets us
        # forward the call to self.cloud.request() so we can also record it.
        #
        # Patch 3 – HAFailOver.get_instance_metadata → fixed dict
        # ---------------------------------------------------------
        # get_instance_metadata() opens a TCP connection to IBM Cloud's IMDS
        # endpoint (api.metadata.cloud.ibm.com) to discover the VPC ID and
        # availability zone.  That only works inside an actual IBM Cloud VSI.
        # We replace it with return_value={"vpc": ..., "zone": ...} so the
        # library thinks it is running on a VSI in us-south-1 inside vpc-1.
        #
        # Patch 4 – lib.v → fixed version string
        # --------------------------------------
        # HAFailOver.__init__ logs f"Version: {v('ibm_cloud_fail_over')}".
        # An f-string is evaluated even when DEBUG is off, and v() is
        # importlib.metadata.version, which raises PackageNotFoundError when
        # the library is run from a source checkout instead of being
        # pip-installed.  Without this patch every HAFailOver() call in these
        # tests would fail on a machine that has not installed the package,
        # which defeats the purpose of a driver that runs anywhere.
        #
        # Patching the same class multiple times is perfectly fine: each
        # patch.object() call targets a different attribute name, so they are
        # independent and do not interfere.
        # -------------------------------------------------------------------
        for p in (
            mock.patch.object(lib, "run",
                              side_effect=fake_run),
            mock.patch.object(lib.HAFailOver, "_make_api_request",
                              side_effect=lambda *a, **k: self.cloud.request(*a, **k)),
            mock.patch.object(lib.HAFailOver, "get_instance_metadata",
                              return_value={"vpc": {"id": "vpc-1"},
                                            "zone": {"name": "us-south-1"}}),
            mock.patch.object(lib, "v", return_value="0.0.0-test"),
        ):
            # -------------------------------------------------------------------
            # mock.patch.object() returns a patcher object – think of it as a
            # recipe for the swap, not the swap itself.  Calling p.start()
            # actually performs the replacement: it saves the original value of
            # the attribute and installs the fake in its place.  From this point
            # forward, any code that reads that attribute gets the fake.
            #
            # self.addCleanup(p.stop) registers p.stop() to be called
            # automatically when the test finishes (pass or fail).  p.stop()
            # undoes the replacement, restoring the original value.  This means
            # each test starts with a clean slate and patches never leak into
            # other tests.
            #
            # The alternative is to use patch as a context manager (with mock.patch
            # ...) or as a decorator (@mock.patch ...), but those only work when
            # the entire test body is in one function.  Because setUp runs before
            # each test method and we want all the patches active throughout,
            # start/addCleanup is the right pattern here.
            # -------------------------------------------------------------------
            p.start()
            self.addCleanup(p.stop)

    # Convenience: call fail_over_cr_vip with sensible defaults.
    def vip_op(self, cmd, vips=",".join(VIPS)):
        return lib.fail_over_cr_vip(
            cmd, "https://us-south.iaas.cloud.ibm.com",
            EXT_IP_1, EXT_IP_2, "key", IFACE, vips,
        )

    # Build the expected list of recorded ip commands for a given operation.
    # op is "add" (SET) or "del" (UNSET); the address is expected in prefixed
    # form because that is what the library passes to 'ip'.
    def expected(self, op, vips=VIPS):
        return [["ip", "a", op, prefix(v), "dev", IFACE] for v in vips]


# ---------------------------------------------------------------------------
# TestUpdateOsRoutes – tests _update_os_iface_vip_addresses in isolation
#
# We instantiate HAFailOver directly and call the method ourselves instead of
# going through fail_over_cr_vip.  This lets us verify the OS-level behaviour
# independently of the cloud routing logic.
# ---------------------------------------------------------------------------
class TestUpdateOsRoutes(Base):
    """Direct calls to HAFailOver._update_os_iface_vip_addresses."""

    def make(self, vips):
        """Create a minimal HAFailOver instance with only the fields
        _update_os_iface_vip_addresses reads."""
        h = lib.HAFailOver()
        h.interface_name = IFACE
        h.vip_addresses  = vips
        return h

    def test_set_adds_each_vip(self):
        # SET should produce 'ip a add <vip>/32 dev <iface>' for every VIP.
        self.make(VIPS)._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_unset_deletes_each_vip(self):
        # UNSET should produce 'ip a del <vip>/32 dev <iface>' for every VIP.
        self.make(VIPS)._update_os_iface_vip_addresses("UNSET")
        self.assertEqual(self.ip_cmds, self.expected("del"))

    def test_empty_list_runs_nothing(self):
        # When vip_addresses is empty the method must not call run at all.
        self.make([])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, [])

    def test_empty_entries_are_skipped(self):
        # fail_over_cr_vip builds the list with vip_addresses.split(','), so a
        # trailing comma or a doubled comma yields '' entries.  Running
        # 'ip a add /32 dev eth0' for those would fail on a real host, so the
        # library filters them out.  Only the one real VIP may produce a
        # command.
        self.make(["", VIPS[0], ""])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, self.expected("add", [VIPS[0]]))

    def test_bare_ip_gets_slash_32(self):
        # A VIP is a single address, so a bare IP must be given a /32 prefix.
        # Without a prefix 'ip' would pick a default that depends on the
        # address class, which could create a surprising connected route.
        self.make([VIPS[0]])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds[0][3], VIPS[0] + "/32")

    def test_entry_with_prefix_is_passed_through_unchanged(self):
        # If the user already supplied a prefix length, the library must not
        # append /32 on top of it (that would give '192.168.100.1/24/32').
        self.make(["192.168.100.1/24"])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds[0][3], "192.168.100.1/24")

    def test_add_rc2_means_already_assigned_and_continues(self):
        # 'ip a add' exits with 2 when the address is already on the
        # interface.  That is the normal situation when start is retried or
        # when Pacemaker re-runs start after a partial failure, so the
        # library logs it and moves on.  Both VIPs must still be attempted and
        # no exception may escape.
        self.ip_rc[prefix(VIPS[0])] = 2
        self.make(VIPS)._update_os_iface_vip_addresses("SET")   # must not raise
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_add_other_failure_raises_and_stops(self):
        # Any non-zero exit other than 2 on 'add' is a real error (bad
        # interface name, no permission, ...).  The library raises
        # CalledProcessError so the caller can fail the resource, and it
        # stops at the first failure: the second VIP is never attempted.
        self.ip_rc[prefix(VIPS[0])] = 1
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            self.make(VIPS)._update_os_iface_vip_addresses("SET")
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertEqual(self.ip_cmds, self.expected("add", VIPS[:1]))

    def test_del_failure_raises_and_stops(self):
        # Same fail-fast rule for UNSET: if removing the first VIP fails, the
        # second is left on the interface and the error propagates.
        self.ip_rc[prefix(VIPS[0])] = 1
        with self.assertRaises(subprocess.CalledProcessError):
            self.make(VIPS)._update_os_iface_vip_addresses("UNSET")
        self.assertEqual(self.ip_cmds, self.expected("del", VIPS[:1]))

    def test_del_rc2_is_not_tolerated_characterization(self):
        # CHARACTERIZATION, not an endorsement.  The 'already assigned'
        # tolerance (rc 2) exists only for 'add'.  For 'del' ANY non-zero exit
        # raises, so UNSET on an interface that no longer has the address
        # (for example a second stop in a row) raises instead of succeeding.
        # OCF requires stop to be idempotent, so this may need to change.
        # I expect 'ip a del' of a missing address to exit with 2 as well, but
        # that is unverified; '--live' mode prints the real exit code.
        # This test pins today's behaviour so a change is a deliberate one.
        self.ip_rc[prefix(VIPS[0])] = 2
        with self.assertRaises(subprocess.CalledProcessError):
            self.make(VIPS)._update_os_iface_vip_addresses("UNSET")

    @unittest.expectedFailure
    def test_whitespace_around_entry_is_stripped(self):
        # KNOWN GAP: entries are not stripped.  A value such as
        # "192.168.100.1, 192.168.100.2" splits into ['192.168.100.1',
        # ' 192.168.100.2'] and the second command is built with a leading
        # space, which 'ip' rejects.  Fix: strip each entry when the list is
        # built or when it is used.
        self.make([VIPS[0], " " + VIPS[1]])._update_os_iface_vip_addresses("SET")
        self.assertEqual(self.ip_cmds, self.expected("add"))


# ---------------------------------------------------------------------------
# TestFailOverCrVip – end to end through the public fail_over_cr_vip function
# ---------------------------------------------------------------------------
class TestFailOverCrVip(Base):
    """End to end through fail_over_cr_vip with a fake cloud."""

    def test_set_patches_route_then_adds_vips(self):
        # SET must flip the cloud route (one PATCH) AND assign the VIPs locally.
        result = self.vip_op("SET")
        self.assertEqual(result, EXT_IP_2)              # next hop now points to Node B
        self.assertEqual(len(self.cloud.patches()), 1)  # exactly one cloud PATCH
        self.assertEqual(self.ip_cmds, self.expected("add"))

    def test_set_with_api_failure_adds_no_vips(self):
        # Order matters: the library updates the cloud route first and assigns
        # the VIPs locally only after that succeeds.  If the cloud rejects the
        # PATCH, this node must NOT end up holding the VIPs, otherwise both
        # nodes could answer for the same address.  The ApiException must
        # reach the caller and no 'ip' command may have run.
        self.cloud.fail_methods = {"PATCH"}
        with self.assertRaises(lib.ApiException):
            self.vip_op("SET")
        self.assertEqual(self.ip_cmds, [])

    def test_get_returns_next_hop_and_touches_nothing(self):
        # GET is read-only: no cloud PATCH, no ip commands.
        result = self.vip_op("GET")
        self.assertEqual(result, EXT_IP_1)
        self.assertEqual(self.ip_cmds, [])
        self.assertEqual(self.cloud.patches(), [])

    def test_unset_deletes_vips(self):
        # UNSET must remove the VIP addresses from the local interface.
        self.vip_op("UNSET")
        self.assertEqual(self.ip_cmds, self.expected("del"))

    def test_unset_should_not_modify_the_cloud_route(self):
        # This used to be an expected failure: UNSET fell into the PATCH
        # branch and flipped the next hop, redirecting traffic away from the
        # node that was about to take over.  update_vpc_routing_table_route now
        # returns early for UNSET, so nothing at all may be sent to the cloud
        # (not even the GETs) and the returned next hop is the empty string.
        result = self.vip_op("UNSET")
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(result, "")

    def test_empty_vip_addresses_runs_no_ip_command(self):
        # This used to be an expected failure: "".split(',') returns [''], a
        # list with one empty string, and the library ran
        # 'ip addr add "" dev eth0'.  Empty entries are now filtered out, so
        # a resource configured without VIPs does only the cloud route update.
        self.vip_op("SET", vips="")
        self.assertEqual(self.ip_cmds, [])
        self.assertEqual(len(self.cloud.patches()), 1)   # route still flipped

    def test_empty_vip_addresses_unset_runs_no_ip_command(self):
        # The same filtering must apply on the way down, otherwise stop would
        # fail for every resource that has no vip_addresses configured.
        self.vip_op("UNSET", vips="")
        self.assertEqual(self.ip_cmds, [])

    @unittest.expectedFailure
    def test_space_separated_vips_are_split(self):
        # KNOWN GAP: the library splits vip_addresses on ',' only (its
        # docstring says "comma-separated"), but the OCF RA convention seen in
        # gcp-vpc-move-vip uses spaces as the delimiter.  The two must agree.
        # Fix: either change the library split to whitespace, or document
        # comma as the only supported delimiter and delete this test.
        self.vip_op("SET", vips="192.168.100.1 192.168.100.2")
        self.assertEqual(self.ip_cmds, self.expected("add"))


# ---------------------------------------------------------------------------
# live() – optional mode that really runs ip a add/del on a Linux host.
# No mocks except the version lookup: calls _update_os_iface_vip_addresses directly and
# checks the kernel address table with 'ip -o addr show' to confirm the
# assignment took effect.
# Requires root (sudo) and a Linux host with the 'ip' command.
# Use TEST-NET addresses (192.0.2.0/24) to avoid conflicts with real traffic.
# ---------------------------------------------------------------------------
def live(iface, vips):
    """Really run ip a add/del on a Linux host and verify with 'ip'."""

    def present(vip):
        """Return True if vip is currently assigned to iface."""
        out = subprocess.run(
            ["ip", "-o", "addr", "show", "dev", iface],
            capture_output=True, text=True, check=True,
        ).stdout
        # 'ip -o addr show' prints one address per line; each line contains
        # the address in CIDR form, e.g. "2: eth0    inet 192.0.2.10/32 ...".
        # An entry given with its own prefix (192.0.2.12/24) must be compared
        # by address only, so drop the prefix before looking for it.
        address = vip.split("/")[0]
        return any(" {}/".format(address) in line for line in out.splitlines())

    def attempt(cmd):
        """Run one _update_os_iface_vip_addresses call and report the exit code of any
        failure instead of letting the exception end the live run."""
        try:
            h._update_os_iface_vip_addresses(cmd)
            return None
        except subprocess.CalledProcessError as exc:
            return exc.returncode

    # HAFailOver.__init__ calls importlib.metadata.version(), which fails when
    # the library is run from a source checkout that is not pip-installed.
    # This is the only thing faked in live mode.
    with mock.patch.object(lib, "v", return_value="live"):
        h = lib.HAFailOver()
    h.interface_name = iface
    h.vip_addresses  = vips
    ok = True

    # Phase 1 – the basic cycle: add everything, then delete everything.
    for step, cmd, want in (("add", "SET", True), ("del", "UNSET", False)):
        rc = attempt(cmd)
        if rc is not None:
            ok = False
            print("FAIL: {} raised, 'ip' exit code {}".format(step, rc))
            continue
        for v in vips:
            got = present(v)
            ok &= (got == want)
            print("{}: after {}, {} on {} present={} (expected {})".format(
                "PASS" if got == want else "FAIL", step, v, iface, got, want))

    # Phase 2 – repeating SET must succeed.  The second 'ip a add' exits with
    # 2 (address exists) and the library is supposed to tolerate that.
    attempt("SET")
    rc = attempt("SET")
    ok &= (rc is None)
    print("{}: second SET did not raise (rc={})".format(
        "PASS" if rc is None else "FAIL", rc))

    # Phase 3 – clean up, then repeat UNSET on the now-empty interface.  This
    # is a finding, not a pass/fail check: it reveals the real 'ip a del' exit
    # code for a missing address, which decides whether stop is idempotent
    # (see test_del_rc2_is_not_tolerated_characterization).
    attempt("UNSET")
    rc = attempt("UNSET")
    if rc is None:
        print("FINDING: second UNSET succeeded; stop is idempotent")
    else:
        print("FINDING: second UNSET raised, 'ip a del' exit code {}; "
              "stop is NOT idempotent for already-removed VIPs".format(rc))

    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--live":
        if len(sys.argv) != 4:
            sys.exit(__doc__)
        sys.exit(live(sys.argv[2], sys.argv[3].split(",")))
    unittest.main(verbosity=2)
