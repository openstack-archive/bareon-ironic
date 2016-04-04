#
# Copyright 2016 Cray Inc., All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ironic.drivers import base
from ironic.drivers.modules import inspector
from ironic.drivers.modules import ipmitool
from ironic.drivers.modules import ssh

from bareon_ironic.modules import bareon_rsync
from bareon_ironic.modules import bareon_swift


class BareonSwiftAndIPMIToolDriver(base.BaseDriver):
    """Bareon Swift + IPMITool driver.

    This driver implements the `core` functionality, combining
    :class:`ironic.drivers.modules.ipmitool.IPMIPower` (for power on/off and
    reboot) with
    :class:`ironic.drivers.modules.bareon_swift.BareonSwiftDeploy`
    (for image deployment).
    Implementations are in those respective classes; this class is merely the
    glue between them.
    """

    def __init__(self):
        self.power = ipmitool.IPMIPower()
        self.deploy = bareon_swift.BareonSwiftDeploy()
        self.management = ipmitool.IPMIManagement()
        self.vendor = bareon_swift.BareonSwiftVendor()
        self.inspect = inspector.Inspector.create_if_enabled(
            'BareonSwiftAndIPMIToolDriver')


class BareonSwiftAndSSHDriver(base.BaseDriver):
    """Bareon Swift + SSH driver.

    NOTE: This driver is meant only for testing environments.

    This driver implements the `core` functionality, combining
    :class:`ironic.drivers.modules.ssh.SSH` (for power on/off and reboot of
    virtual machines tunneled over SSH), with
    :class:`ironic.drivers.modules.bareon_swift.BareonSwiftDeploy`
    (for image deployment). Implementations are in those respective classes;
    this class is merely the glue between them.
    """

    def __init__(self):
        self.power = ssh.SSHPower()
        self.deploy = bareon_swift.BareonSwiftDeploy()
        self.management = ssh.SSHManagement()
        self.vendor = bareon_swift.BareonSwiftVendor()
        self.inspect = inspector.Inspector.create_if_enabled(
            'BareonSwiftAndSSHDriver')


class BareonRsyncAndIPMIToolDriver(base.BaseDriver):
    """Bareon Rsync + IPMITool driver.

    This driver implements the `core` functionality, combining
    :class:`ironic.drivers.modules.ipmitool.IPMIPower` (for power on/off and
    reboot) with
    :class:`ironic.drivers.modules.bareon_rsync.BareonRsyncDeploy`
    (for image deployment).
    Implementations are in those respective classes; this class is merely the
    glue between them.
    """

    def __init__(self):
        self.power = ipmitool.IPMIPower()
        self.deploy = bareon_rsync.BareonRsyncDeploy()
        self.management = ipmitool.IPMIManagement()
        self.vendor = bareon_rsync.BareonRsyncVendor()
        self.inspect = inspector.Inspector.create_if_enabled(
            'BareonRsyncAndIPMIToolDriver')


class BareonRsyncAndSSHDriver(base.BaseDriver):
    """Bareon Rsync + SSH driver.

    NOTE: This driver is meant only for testing environments.

    This driver implements the `core` functionality, combining
    :class:`ironic.drivers.modules.ssh.SSH` (for power on/off and reboot of
    virtual machines tunneled over SSH), with
    :class:`ironic.drivers.modules.bareon_rsync.BareonRsyncDeploy`
    (for image deployment). Implementations are in those respective classes;
    this class is merely the glue between them.
    """

    def __init__(self):
        self.power = ssh.SSHPower()
        self.deploy = bareon_rsync.BareonRsyncDeploy()
        self.management = ssh.SSHManagement()
        self.vendor = bareon_rsync.BareonRsyncVendor()
        self.inspect = inspector.Inspector.create_if_enabled(
            'BareonRsyncAndSSHDriver')
