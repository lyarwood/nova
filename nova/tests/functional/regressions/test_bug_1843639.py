# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import mock
import time

from nova.objects import migrate_data as migrate_data_obj
from nova.tests import fixtures as nova_fixtures
from nova.tests.functional import fixtures as func_fixtures
from nova.tests.functional import integrated_helpers
from nova.tests.functional.libvirt import base
from nova.tests.unit import cast_as_call
from nova.tests.unit import policy_fixture
from nova.tests.unit.virt.libvirt import fakelibvirt

from nova.virt.libvirt import config as vconfig
from nova.virt.libvirt import guest as libvirt_guest
from nova.virt.libvirt.volume import volume as libvirt_volume

from oslo_concurrency import processutils


class TestPostLiveMigrationVolumeDisconnectErrror(base.ServersTestBase,
        integrated_helpers.InstanceHelperMixin):

    def setUp(self):
        super(TestPostLiveMigrationVolumeDisconnectErrror, self).setUp()

        self.useFixture(policy_fixture.RealPolicyFixture())
        self.useFixture(nova_fixtures.NeutronFixture(self))
        self.useFixture(func_fixtures.PlacementFixture())
        self.useFixture(nova_fixtures.CinderFixture(self))
        self.useFixture(cast_as_call.CastAsCall(self))

        api_fixture = self.useFixture(nova_fixtures.OSAPIFixture(
            api_version='v2.1'))

        self.admin_api = api_fixture.admin_api
        self.api = api_fixture.api

        # Use microversion 2.74 so we can place the instance on the src node
        self.admin_api.microversion = '2.74'

        self.start_service('conductor')
        self.start_service('scheduler')

        # Ensure the libvirt fixture returns some useful host info
        host_info = fakelibvirt.HostInfo(cpu_nodes=2, cpu_sockets=1,
                                         cpu_cores=2, cpu_threads=2,
                                         kB_mem=1574000)
        self.mock_conn.return_value = self._get_connection(host_info=host_info)

        # Start two compute nodes using the libvirt driver
        self.flags(compute_driver='libvirt.LibvirtDriver')
        self.compute_src = self.start_service('compute', host='src')
        self.compute_dest = self.start_service('compute', host='dest')

    @mock.patch('nova.virt.libvirt.guest.Guest.migrate')
    @mock.patch('nova.compute.manager.ComputeManager.pre_live_migration')
    @mock.patch('nova.compute.manager.ComputeManager'
                '.check_can_live_migrate_destination')
    @mock.patch('nova.virt.libvirt.LibvirtDriver._get_volume_driver')
    @mock.patch('nova.virt.libvirt.LibvirtDriver.get_volume_connector')
    @mock.patch('nova.virt.libvirt.guest.Guest.get_job_info')
    def test_live_migrate(self, mock_get_job_info, mock_get_volume_connector,
                          mock_get_volume_driver, mock_dest_check,
                          mock_pre_lm, mock_migrate):

        # Mock out the volume driver and ensure it fails on disconnect_volume
        mock_volume_driver = mock.MagicMock(
            spec=libvirt_volume.LibvirtBaseVolumeDriver)
        mock_volume_driver.disconnect_volume.side_effect = \
            processutils.ProcessExecutionError
        fake_disk = vconfig.LibvirtConfigGuestDisk()
        fake_disk.driver_name = "fake"
        fake_disk.type = "block"
        fake_disk.source_device = "/dev/fake"
        fake_disk.source_path = "/dev/fake"
        fake_disk.target_dev = "/dev/fake"
        fake_disk.target_bus = "scsi"
        mock_volume_driver.get_config.return_value = fake_disk
        mock_get_volume_driver.return_value = mock_volume_driver

        # Mock out some of the LM checks and return some fake LM data
        migrate_data = migrate_data_obj.LibvirtLiveMigrateData()
        migrate_data.is_shared_instance_path = False
        migrate_data.is_shared_block_storage = False
        migrate_data.bdms = []
        migrate_data.vifs = []
        migrate_data.block_migration = False
        migrate_data.serial_listen_addr = "1.2.3.4"
        migrate_data.serial_listen_ports = [8080]
        migrate_data.instance_relative_path = "/fake/"
        mock_dest_check.return_value = migrate_data
        mock_pre_lm.return_value = migrate_data

        # Allow the LM job to complete immediately
        mock_get_job_info.return_value = libvirt_guest.JobInfo(
                    type=fakelibvirt.VIR_DOMAIN_JOB_COMPLETED)

        # Boot a volume backed instance
        server_req = {
            'name': 'server',
            'flavorRef': self.api.get_flavors()[0]['id'],
            'host': 'src',
            'networks': 'none',
            'block_device_mapping_v2': [{
                'boot_index': 0,
                'uuid': nova_fixtures.CinderFixture.IMAGE_BACKED_VOL,
                'source_type': 'volume',
                'destination_type': 'volume'}],
        }
        server = self.admin_api.post_server({'server': server_req})
        server_id = server['id']
        self.wait_till_active_or_timeout(server_id)

        # live migrate the instance
        self.admin_api.post_server_action(server_id,
            {"os-migrateLive": {
                "block_migration": False,
                "host": "dest",
            }})

        # FIXME (lyarwood): The instance should be ACTIVE with the migration
        # complete and running on the dest even after a disconnect_volume
        # failure during LM.
        self._wait_for_state_change(server, 'ERROR')
        self._wait_for_migration_status(server, ['error'])
        server = self.admin_api.get_server(server_id)
        self.assertEqual('src', server['OS-EXT-SRV-ATTR:host'])

        # Ensure we actually called disconnect_volume
        mock_volume_driver.disconnect_volume.assert_called_once_with(mock.ANY,
                                                                     mock.ANY)

    # WIP as I can't remove this without the tests breaking, specifically the
    # mocking of get_volume_connector?!! I have no idea why.....
    def wait_till_active_or_timeout(self, server_id):
        timeout = 0.0
        server = self.api.get_server(server_id)
        while server['status'] != "ACTIVE" and timeout < 10.0:
            time.sleep(.1)
            timeout += .1
            server = self.api.get_server(server_id)
        if server['status'] != "ACTIVE":
            self.fail("The server is not active after the timeout.")
