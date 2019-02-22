# Copyright (c) 2018 Intel, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import fixtures
import mock

import os_traits as ost

from nova import conf
from nova import test
from nova.tests.functional import integrated_helpers
from nova.tests.unit.virt.libvirt import fakelibvirt
from nova.virt.libvirt.driver import SEV_KERNEL_PARAM_FILE

CONF = conf.CONF


class LibvirtReportTraitsTestBase(
        integrated_helpers.ProviderUsageBaseTestCase):
    compute_driver = 'libvirt.LibvirtDriver'

    def setUp(self, init_host=False):
        super(LibvirtReportTraitsTestBase, self).setUp()
        self.useFixture(fakelibvirt.FakeLibvirtFixture(stub_os_vif=False))
        if not init_host:
            self.useFixture(
                fixtures.MockPatch(
                    'nova.virt.libvirt.driver.LibvirtDriver.init_host'))
        self.assertEqual([], self._get_all_providers())
        self.start_compute()

    def start_compute(self):
        self.compute = self._start_compute(CONF.host)
        nodename = self.compute.manager._get_nodename(None)
        self.host_uuid = self._get_provider_uuid_by_host(nodename)


class LibvirtReportTraitsTests(LibvirtReportTraitsTestBase):
    def test_report_cpu_traits(self):
        # Test CPU traits reported on initial node startup, these specific
        # trait values are coming from fakelibvirt's baselineCPU result.
        traits = self._get_provider_traits(self.host_uuid)
        for trait in ('HW_CPU_X86_VMX', 'HW_CPU_X86_AESNI'):
            self.assertIn(trait, traits)

        self._create_trait('CUSTOM_TRAITS')
        new_traits = ['CUSTOM_TRAITS', 'HW_CPU_X86_AVX']
        self._set_provider_traits(self.host_uuid, new_traits)
        # The above is an out-of-band placement operation, as if the operator
        # used the CLI. So now we have to "SIGHUP the compute process" to clear
        # the report client cache so the subsequent update picks up the change.
        self.compute.manager.reset()
        self._run_periodics()
        # HW_CPU_X86_AVX is filtered out because nova-compute owns CPU traits
        # and it's not in the baseline for the host.
        traits = set(self._get_provider_traits(self.host_uuid))
        expected_traits = self.expected_libvirt_driver_capability_traits.union(
            [u'HW_CPU_X86_VMX', u'HW_CPU_X86_AESNI', u'CUSTOM_TRAITS']
        )
        self.assertItemsEqual(expected_traits, traits)


class LibvirtReportNoSevTraitsTests(LibvirtReportTraitsTestBase):
    @test.patch_exists(SEV_KERNEL_PARAM_FILE, False)
    def setUp(self):
        super(LibvirtReportNoSevTraitsTests, self).setUp(True)

    def test_sev_trait_off_on(self):
        """Test that the compute service reports the SEV trait and register it
        on the compute host resource provider in the placement API.

        Then test that if the SEV capability appears, after a restart
        of the compute service, the trait gets registered on the
        compute host.
        """
        sev_trait = ost.HW_CPU_AMD_SEV

        global_traits = self._get_all_traits()
        self.assertIn(sev_trait, global_traits)

        traits = self._get_provider_traits(self.host_uuid)
        self.assertNotIn(sev_trait, traits)

        # Now simulate the host gaining SEV functionality (e.g. via a
        # libvirtd upgrade).
        sev_features = \
            fakelibvirt.virConnect._domain_capability_features_with_SEV
        with test.nested(
                self.patch_exists(SEV_KERNEL_PARAM_FILE, True),
                self.patch_open(SEV_KERNEL_PARAM_FILE, "1\n"),
                mock.patch.object(fakelibvirt.virConnect,
                                  '_domain_capability_features',
                                  new=sev_features)
        ) as (mock_exists, mock_open, mock_features):
            # Now when we re-init the compute service, the driver's
            # init_host() should discover that the SEV capability has
            # appeared:
            self.compute.driver.init_host('dummyhost')
            mock_exists.assert_has_calls([mock.call(SEV_KERNEL_PARAM_FILE)])
            mock_open.assert_has_calls([mock.call(SEV_KERNEL_PARAM_FILE)])

            # However it won't disappear in the provider tree and get synced
            # back to placement until we force a reinventory:
            # placement.
            self.compute.manager.reset()
            self._run_periodics()

            traits = self._get_provider_traits(self.host_uuid)
            self.assertIn(sev_trait, traits)

            global_traits = self._get_all_traits()
            self.assertIn(sev_trait, global_traits)


class LibvirtReportSevTraitsTests(LibvirtReportTraitsTestBase):
    @test.patch_exists(SEV_KERNEL_PARAM_FILE, True)
    @test.patch_open(SEV_KERNEL_PARAM_FILE, "1\n")
    @mock.patch.object(fakelibvirt.virConnect, '_domain_capability_features',
        new=fakelibvirt.virConnect._domain_capability_features_with_SEV)
    def setUp(self):
        super(LibvirtReportSevTraitsTests, self).setUp(True)

    def test_sev_trait_on_off(self):
        """Test that the compute service reports the SEV trait and registers it
        on the compute host resource provider in the placement API.

        Then test that if the SEV capability vanishes, after a restart
        of the compute service, the trait gets removed from the
        compute host.
        """
        sev_trait = ost.HW_CPU_AMD_SEV

        global_traits = self._get_all_traits()
        self.assertIn(sev_trait, global_traits)

        traits = self._get_provider_traits(self.host_uuid)
        self.assertIn(sev_trait, traits)

        # Now simulate the host losing SEV functionality (e.g. via a
        # libvirtd downgrade).
        with self.patch_exists(SEV_KERNEL_PARAM_FILE, False) as mock_exists:
            # Now when we re-init the compute service, the driver's
            # init_host() should discover that the SEV capability has
            # vanished:
            self.compute.driver.init_host('dummyhost')
            mock_exists.assert_has_calls([mock.call(SEV_KERNEL_PARAM_FILE)])

            # However it won't disappear in the provider tree and get synced
            # back to placement until we force a reinventory:
            self.compute.manager.reset()
            self._run_periodics()

            traits = self._get_provider_traits(self.host_uuid)
            self.assertNotIn(sev_trait, traits)

            # Sanity check that we've still got the trait globally.
            global_traits = self._get_all_traits()
            self.assertIn(sev_trait, global_traits)
