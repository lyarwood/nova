# Copyright 2016 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

import copy
import mock

import fixtures
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils

from nova import context
from nova import objects

from nova import test
from nova.tests.functional.test_servers import ServersTestBase
from nova.tests.unit.api.openstack import fakes
from nova.tests.unit import fake_network
from nova.tests.unit.virt.libvirt import fake_libvirt_utils
from nova.tests.unit.virt.libvirt import fakelibvirt


CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class ValumeRaceConditonTest(ServersTestBase):

    def setUp(self):
        super(ValumeRaceConditonTest, self).setUp()

        # Replace libvirt with fakelibvirt
        self.useFixture(fixtures.MonkeyPatch(
           'nova.virt.libvirt.driver.libvirt_utils',
           fake_libvirt_utils))
        self.useFixture(fixtures.MonkeyPatch(
           'nova.virt.libvirt.driver.libvirt',
           fakelibvirt))
        self.useFixture(fixtures.MonkeyPatch(
           'nova.virt.libvirt.host.libvirt',
           fakelibvirt))
        self.useFixture(fixtures.MonkeyPatch(
           'nova.virt.libvirt.guest.libvirt',
           fakelibvirt))
        self.useFixture(fakelibvirt.FakeLibvirtFixture())
        self.flags(sysinfo_serial='none', group='libvirt')

    def _setup_compute_service(self):
        self.flags(compute_driver='libvirt.LibvirtDriver')

    @mock.patch('nova.virt.libvirt.LibvirtDriver._create_image')
    @mock.patch('nova.utils.execute')
    def test_volume_attachment_race_condition(self, mock_execute, img_mock):
        self.stub_out('nova.volume.cinder.API.get', fakes.stub_volume_get)
        self.stub_out('nova.volume.cinder.API.check_attach',
                      lambda *a, **k: None)
        self.stub_out('nova.volume.cinder.API.check_detach',
                      lambda *a, **k: None)
        self.stub_out('nova.volume.cinder.API.begin_detaching',
                      lambda *a, **k: None)

        self.stub_out('nova.volume.cinder.API.reserve_volume',
                      lambda *a, **k: None)
        connection_info = {"driver_volume_type": "nfs",
                           "data": {"export": "192.168.1.1:/nfs/share1",
                                    "name": "volume2"}}
        self.stub_out('nova.volume.cinder.API.initialize_connection',
                      lambda *a, **k: connection_info)
        self.stub_out('nova.volume.cinder.API.terminate_connection',
                      lambda *a, **k: None)
        self.stub_out('nova.volume.cinder.API.detach',
                      lambda *a, **k: None)

        volume = fakes.stub_volume_get(None, context.get_admin_context(),
                                       'a26887c6-c47b-4654-abb5-dfadf7d3f803')

        bdm = objects.BlockDeviceMapping()
        device_name = '/dev/vdd'
        bdm['id'] = 1
        bdm['volume_id'] = volume['id']
        bdm['source_type'] = 'volume'
        bdm['no_device'] = False
        bdm['connection_info'] = None
        bdm['disk_bus'] = 'virtio'
        bdm['boot_index'] = -1
        bdm['volume_size'] = 1
        bdm['device_name'] = device_name
        bdm['destination_type'] = 'volume'
        bdm['device_type'] = 'disk'
        self.stub_out(
            'nova.compute.manager.ComputeManager.reserve_block_device_name',
            lambda *a, **k: bdm)

        detach_bdm = copy.deepcopy(bdm)
        detach_bdm['connection_info'] = jsonutils.dumps(connection_info)
        self.stub_out(
            'nova.objects.BlockDeviceMapping.get_by_volume_and_instance',
            classmethod(lambda *a, **k: detach_bdm))
        self.stub_out('nova.objects.BlockDeviceMapping.destroy',
                      lambda *a, **k: None)

        subs = {
            'volumeId': volume['id'],
            'device': device_name
        }
        mount_path = 'fake_mount_path'
        existed_device = ['%s/volume1' % mount_path]
        fake_connection = fakelibvirt.Connection('qemu:///system',
                                                 version=1002013,
                                                 hv_version=2001000)
        with test.nested(
            mock.patch('nova.virt.libvirt.host.Host.get_connection',
                       return_value=fake_connection),
            mock.patch('nova.virt.libvirt.LibvirtDriver._get_all_file_devices',
                       return_value=existed_device),
            mock.patch('nova.virt.libvirt.volume.nfs.LibvirtNFSVolumeDriver'
                       '._unmount_nfs'),
            mock.patch('nova.virt.libvirt.volume.fs.'
                       'LibvirtBaseFileSystemVolumeDriver._get_mount_path',
                       return_value=mount_path)
            )as (conn_mock, file_mock, unmount_mock, path_mock):
            self.compute = self.start_service('compute', host='test_compute0')
            fake_network.set_stub_network_methods(self)

            flavor = self._create_flavor()
            server = self._build_server(flavor)
            created = self.api.post_server({'server': server})

            instance = self.api.get_server(created['id'])
            self._wait_for_state_change(instance, 'BUILD')

            response = self.api.post_server_volume(created['id'],
                  {'volumeAttachment': subs})

            self.api.delete_server_volume(created['id'],
                                          response['id'])
            unmount_mock.assert_not_called()

            self.api.delete_server(created['id'])
