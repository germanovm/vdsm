#
# Copyright 2012 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import calendar
import logging
import socket
import time
import xml.etree.cElementTree as etree

from vdsm import utils
from vdsm import netinfo
import exception as ge
from . import makePublic, makePublicRHEV

_glusterCommandPath = utils.CommandPath("gluster",
                                        "/usr/sbin/gluster",
                                        )
_TIME_ZONE = time.tzname[0]


if hasattr(etree, 'ParseError'):
    _etreeExceptions = (etree.ParseError, AttributeError, ValueError)
else:
    _etreeExceptions = (SyntaxError, AttributeError, ValueError)


def _getGlusterVolCmd():
    return [_glusterCommandPath.cmd, "--mode=script", "volume"]


def _getGlusterPeerCmd():
    return [_glusterCommandPath.cmd, "--mode=script", "peer"]


def _getGlusterSystemCmd():
    return [_glusterCommandPath.cmd, "system::"]


def _getGlusterVolGeoRepCmd():
    return _getGlusterVolCmd() + ["geo-replication"]


def _getGlusterSnapshotCmd():
    return [_glusterCommandPath.cmd, "--mode=script", "snapshot"]


class BrickStatus:
    PAUSED = 'PAUSED'
    COMPLETED = 'COMPLETED'
    RUNNING = 'RUNNING'
    UNKNOWN = 'UNKNOWN'
    NA = 'NA'


class HostStatus:
    CONNECTED = 'CONNECTED'
    DISCONNECTED = 'DISCONNECTED'
    UNKNOWN = 'UNKNOWN'


class VolumeStatus:
    ONLINE = 'ONLINE'
    OFFLINE = 'OFFLINE'


class TransportType:
    TCP = 'TCP'
    RDMA = 'RDMA'


class TaskType:
    REBALANCE = 'REBALANCE'
    REPLACE_BRICK = 'REPLACE_BRICK'
    REMOVE_BRICK = 'REMOVE_BRICK'


class SnapshotStatus:
    ACTIVATED = 'ACTIVATED'
    DEACTIVATED = 'DEACTIVATED'


def _execGluster(cmd):
    return utils.execCmd(cmd)


def _execGlusterXml(cmd):
    cmd.append('--xml')
    rc, out, err = utils.execCmd(cmd)
    if rc != 0:
        raise ge.GlusterCmdExecFailedException(rc, out, err)
    try:
        tree = etree.fromstring('\n'.join(out))
        rv = int(tree.find('opRet').text)
        msg = tree.find('opErrstr').text
        errNo = int(tree.find('opErrno').text)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=out)
    if rv == 0:
        return tree
    else:
        if errNo != 0:
            rv = errNo
        raise ge.GlusterCmdFailedException(rc=rv, err=[msg])


def _getLocalIpAddress():
    for ip in netinfo.getIpAddresses():
        if not ip.startswith('127.'):
            return ip
    return ''


def _getGlusterHostName():
    try:
        return socket.getfqdn()
    except socket.herror:
        logging.exception('getfqdn')
        return ''


@makePublic
def hostUUIDGet():
    command = _getGlusterSystemCmd() + ["uuid", "get"]
    rc, out, err = _execGluster(command)
    if rc == 0:
        for line in out:
            if line.startswith('UUID: '):
                return line[6:]

    raise ge.GlusterHostUUIDNotFoundException()


def _parseVolumeStatus(tree):
    status = {'name': tree.find('volStatus/volumes/volume/volName').text,
              'bricks': [],
              'nfs': [],
              'shd': []}
    hostname = _getLocalIpAddress() or _getGlusterHostName()
    for el in tree.findall('volStatus/volumes/volume/node'):
        value = {}

        for ch in el.getchildren():
            value[ch.tag] = ch.text or ''

        ports = {}
        for ch in el.find('ports').getchildren():
            ports[ch.tag] = ch.text or ''

        if value['path'] == 'localhost':
            value['path'] = hostname

        if value['status'] == '1':
            value['status'] = 'ONLINE'
        else:
            value['status'] = 'OFFLINE'

        if value['hostname'] == 'NFS Server':
            status['nfs'].append({'hostname': value['path'],
                                  'hostuuid': value['peerid'],
                                  'port': ports['tcp'],
                                  'rdma_port': ports['rdma'],
                                  'status': value['status'],
                                  'pid': value['pid']})
        elif value['hostname'] == 'Self-heal Daemon':
            status['shd'].append({'hostname': value['path'],
                                  'hostuuid': value['peerid'],
                                  'status': value['status'],
                                  'pid': value['pid']})
        else:
            status['bricks'].append({'brick': '%s:%s' % (value['hostname'],
                                                         value['path']),
                                     'hostuuid': value['peerid'],
                                     'port': ports['tcp'],
                                     'rdma_port': ports['rdma'],
                                     'status': value['status'],
                                     'pid': value['pid']})
    return status


def _parseVolumeStatusDetail(tree):
    status = {'name': tree.find('volStatus/volumes/volume/volName').text,
              'bricks': []}
    for el in tree.findall('volStatus/volumes/volume/node'):
        value = {}

        for ch in el.getchildren():
            value[ch.tag] = ch.text or ''

        sizeTotal = int(value['sizeTotal'])
        value['sizeTotal'] = sizeTotal / (1024.0 * 1024.0)
        sizeFree = int(value['sizeFree'])
        value['sizeFree'] = sizeFree / (1024.0 * 1024.0)
        status['bricks'].append({'brick': '%s:%s' % (value['hostname'],
                                                     value['path']),
                                 'hostuuid': value['peerid'],
                                 'sizeTotal': '%.3f' % (value['sizeTotal'],),
                                 'sizeFree': '%.3f' % (value['sizeFree'],),
                                 'device': value['device'],
                                 'blockSize': value['blockSize'],
                                 'mntOptions': value['mntOptions'],
                                 'fsName': value['fsName']})
    return status


def _parseVolumeStatusClients(tree):
    status = {'name': tree.find('volStatus/volumes/volume/volName').text,
              'bricks': []}
    for el in tree.findall('volStatus/volumes/volume/node'):
        hostname = el.find('hostname').text
        path = el.find('path').text
        hostuuid = el.find('peerid').text

        clientsStatus = []
        for c in el.findall('clientsStatus/client'):
            clientValue = {}
            for ch in c.getchildren():
                clientValue[ch.tag] = ch.text or ''
            clientsStatus.append({'hostname': clientValue['hostname'],
                                  'bytesRead': clientValue['bytesRead'],
                                  'bytesWrite': clientValue['bytesWrite']})

        status['bricks'].append({'brick': '%s:%s' % (hostname, path),
                                 'hostuuid': hostuuid,
                                 'clientsStatus': clientsStatus})
    return status


def _parseVolumeStatusMem(tree):
    status = {'name': tree.find('volStatus/volumes/volume/volName').text,
              'bricks': []}
    for el in tree.findall('volStatus/volumes/volume/node'):
        brick = {'brick': '%s:%s' % (el.find('hostname').text,
                                     el.find('path').text),
                 'hostuuid': el.find('peerid').text,
                 'mallinfo': {},
                 'mempool': []}

        for ch in el.find('memStatus/mallinfo').getchildren():
            brick['mallinfo'][ch.tag] = ch.text or ''

        for c in el.findall('memStatus/mempool/pool'):
            mempool = {}
            for ch in c.getchildren():
                mempool[ch.tag] = ch.text or ''
            brick['mempool'].append(mempool)

        status['bricks'].append(brick)
    return status


@makePublic
def volumeStatus(volumeName, brick=None, option=None):
    """
    Get volume status

    Arguments:
       * VolumeName
       * brick
       * option = 'detail' or 'clients' or 'mem' or None
    Returns:
       When option=None,
         {'name': NAME,
          'bricks': [{'brick': BRICK,
                      'hostuuid': UUID,
                      'port': PORT,
                      'rdma_port': RDMA_PORT,
                      'status': STATUS,
                      'pid': PID}, ...],
          'nfs': [{'hostname': HOST,
                   'hostuuid': UUID,
                   'port': PORT,
                   'rdma_port': RDMA_PORT,
                   'status': STATUS,
                   'pid': PID}, ...],
          'shd: [{'hostname': HOST,
                  'hostuuid': UUID,
                  'status': STATUS,
                  'pid': PID}, ...]}

      When option='detail',
         {'name': NAME,
          'bricks': [{'brick': BRICK,
                      'hostuuid': UUID,
                      'sizeTotal': SIZE,
                      'sizeFree': FREESIZE,
                      'device': DEVICE,
                      'blockSize': BLOCKSIZE,
                      'mntOptions': MOUNTOPTIONS,
                      'fsName': FSTYPE}, ...]}

       When option='clients':
         {'name': NAME,
          'bricks': [{'brick': BRICK,
                      'hostuuid': UUID,
                      'clientsStatus': [{'hostname': HOST,
                                         'bytesRead': BYTESREAD,
                                         'bytesWrite': BYTESWRITE}, ...]},
                    ...]}

       When option='mem':
         {'name': NAME,
          'bricks': [{'brick': BRICK,
                      'hostuuid': UUID,
                      'mallinfo': {'arena': int,
                                   'fordblks': int,
                                   'fsmblks': int,
                                   'hblkhd': int,
                                   'hblks': int,
                                   'keepcost': int,
                                   'ordblks': int,
                                   'smblks': int,
                                   'uordblks': int,
                                   'usmblks': int},
                      'mempool': [{'allocCount': int,
                                   'coldCount': int,
                                   'hotCount': int,
                                   'maxAlloc': int,
                                   'maxStdAlloc': int,
                                   'name': NAME,
                                   'padddedSizeOf': int,
                                   'poolMisses': int},...]}, ...]}
    """
    command = _getGlusterVolCmd() + ["status", volumeName]
    if brick:
        command.append(brick)
    if option:
        command.append(option)
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeStatusFailedException(rc=e.rc, err=e.err)
    try:
        if option == 'detail':
            return _parseVolumeStatusDetail(xmltree)
        elif option == 'clients':
            return _parseVolumeStatusClients(xmltree)
        elif option == 'mem':
            return _parseVolumeStatusMem(xmltree)
        else:
            return _parseVolumeStatus(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


def _parseVolumeInfo(tree):
    """
        {VOLUMENAME: {'brickCount': BRICKCOUNT,
                      'bricks': [BRICK1, BRICK2, ...],
                      'options': {OPTION: VALUE, ...},
                      'transportType': [TCP,RDMA, ...],
                      'uuid': UUID,
                      'volumeName': NAME,
                      'volumeStatus': STATUS,
                      'volumeType': TYPE,
                      'disperseCount': DISPERSE_COUNT,
                      'redundancyCount': REDUNDANCY_COUNT}, ...}
    """
    volumes = {}
    for el in tree.findall('volInfo/volumes/volume'):
        value = {}
        value['volumeName'] = el.find('name').text
        value['uuid'] = el.find('id').text
        value['volumeType'] = el.find('typeStr').text.upper().replace('-', '_')
        status = el.find('statusStr').text.upper()
        if status == 'STARTED':
            value["volumeStatus"] = VolumeStatus.ONLINE
        else:
            value["volumeStatus"] = VolumeStatus.OFFLINE
        value['brickCount'] = el.find('brickCount').text
        value['distCount'] = el.find('distCount').text
        value['stripeCount'] = el.find('stripeCount').text
        value['replicaCount'] = el.find('replicaCount').text
        value['disperseCount'] = el.find('disperseCount').text
        value['redundancyCount'] = el.find('redundancyCount').text
        transportType = el.find('transport').text
        if transportType == '0':
            value['transportType'] = [TransportType.TCP]
        elif transportType == '1':
            value['transportType'] = [TransportType.RDMA]
        else:
            value['transportType'] = [TransportType.TCP, TransportType.RDMA]
        value['bricks'] = []
        value['options'] = {}
        value['bricksInfo'] = []
        for b in el.findall('bricks/brick'):
            value['bricks'].append(b.text)
        for o in el.findall('options/option'):
            value['options'][o.find('name').text] = o.find('value').text
        for d in el.findall('bricks/brick'):
            brickDetail = {}
            # this try block is to maintain backward compatibility
            # it returns an empty list when gluster doesnot return uuid
            try:
                brickDetail['name'] = d.find('name').text
                brickDetail['hostUuid'] = d.find('hostUuid').text
                value['bricksInfo'].append(brickDetail)
            except AttributeError:
                break
        volumes[value['volumeName']] = value
    return volumes


def _parseVolumeProfileInfo(tree, nfs):
    bricks = []
    if nfs:
        brickKey = 'nfs'
        bricksKey = 'nfsServers'
    else:
        brickKey = 'brick'
        bricksKey = 'bricks'
    for brick in tree.findall('volProfile/brick'):
        fopCumulative = []
        blkCumulative = []
        fopInterval = []
        blkInterval = []
        brickName = brick.find('brickName').text
        if brickName == 'localhost':
            brickName = _getLocalIpAddress() or _getGlusterHostName()
        for block in brick.findall('cumulativeStats/blockStats/block'):
            blkCumulative.append({'size': block.find('size').text,
                                  'read': block.find('reads').text,
                                  'write': block.find('writes').text})
        for fop in brick.findall('cumulativeStats/fopStats/fop'):
            fopCumulative.append({'name': fop.find('name').text,
                                  'hits': fop.find('hits').text,
                                  'latencyAvg': fop.find('avgLatency').text,
                                  'latencyMin': fop.find('minLatency').text,
                                  'latencyMax': fop.find('maxLatency').text})
        for block in brick.findall('intervalStats/blockStats/block'):
            blkInterval.append({'size': block.find('size').text,
                                'read': block.find('reads').text,
                                'write': block.find('writes').text})
        for fop in brick.findall('intervalStats/fopStats/fop'):
            fopInterval.append({'name': fop.find('name').text,
                                'hits': fop.find('hits').text,
                                'latencyAvg': fop.find('avgLatency').text,
                                'latencyMin': fop.find('minLatency').text,
                                'latencyMax': fop.find('maxLatency').text})
        bricks.append(
            {brickKey: brickName,
             'cumulativeStats': {
                 'blockStats': blkCumulative,
                 'fopStats': fopCumulative,
                 'duration': brick.find('cumulativeStats/duration').text,
                 'totalRead': brick.find('cumulativeStats/totalRead').text,
                 'totalWrite': brick.find('cumulativeStats/totalWrite').text},
             'intervalStats': {
                 'blockStats': blkInterval,
                 'fopStats': fopInterval,
                 'duration': brick.find('intervalStats/duration').text,
                 'totalRead': brick.find('intervalStats/totalRead').text,
                 'totalWrite': brick.find('intervalStats/totalWrite').text}})
    status = {'volumeName': tree.find("volProfile/volname").text,
              bricksKey: bricks}
    return status


@makePublic
@makePublicRHEV
def volumeInfo(volumeName=None, remoteServer=None):
    """
    Returns:
        {VOLUMENAME: {'brickCount': BRICKCOUNT,
                      'bricks': [BRICK1, BRICK2, ...],
                      'options': {OPTION: VALUE, ...},
                      'transportType': [TCP,RDMA, ...],
                      'uuid': UUID,
                      'volumeName': NAME,
                      'volumeStatus': STATUS,
                      'volumeType': TYPE}, ...}
    """
    command = _getGlusterVolCmd() + ["info"]
    if remoteServer:
        command += ['--remote-host=%s' % remoteServer]
    if volumeName:
        command.append(volumeName)
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumesListFailedException(rc=e.rc, err=e.err)
    try:
        return _parseVolumeInfo(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeCreate(volumeName, brickList, replicaCount=0, stripeCount=0,
                 transportList=[], force=False):
    command = _getGlusterVolCmd() + ["create", volumeName]
    if stripeCount:
        command += ["stripe", "%s" % stripeCount]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    if transportList:
        command += ["transport", ','.join(transportList)]
    command += brickList

    if force:
        command.append('force')

    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeCreateFailedException(rc=e.rc, err=e.err)
    try:
        return {'uuid': xmltree.find('volCreate/volume/id').text}
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeStart(volumeName, force=False):
    command = _getGlusterVolCmd() + ["start", volumeName]
    if force:
        command.append('force')
    rc, out, err = _execGluster(command)
    if rc:
        raise ge.GlusterVolumeStartFailedException(rc, out, err)
    else:
        return True


@makePublic
def volumeStop(volumeName, force=False):
    command = _getGlusterVolCmd() + ["stop", volumeName]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeStopFailedException(rc=e.rc, err=e.err)


@makePublic
def volumeDelete(volumeName):
    command = _getGlusterVolCmd() + ["delete", volumeName]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeDeleteFailedException(rc=e.rc, err=e.err)


@makePublic
def volumeSet(volumeName, option, value):
    command = _getGlusterVolCmd() + ["set", volumeName, option, value]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeSetFailedException(rc=e.rc, err=e.err)


def _parseVolumeSetHelpXml(out):
    optionList = []
    tree = etree.fromstring('\n'.join(out))
    for el in tree.findall('option'):
        option = {}
        for ch in el.getchildren():
            option[ch.tag] = ch.text or ''
        optionList.append(option)
    return optionList


@makePublic
def volumeSetHelpXml():
    rc, out, err = _execGluster(_getGlusterVolCmd() + ["set", 'help-xml'])
    if rc:
        raise ge.GlusterVolumeSetHelpXmlFailedException(rc, out, err)
    else:
        return _parseVolumeSetHelpXml(out)


@makePublic
def volumeReset(volumeName, option='', force=False):
    command = _getGlusterVolCmd() + ['reset', volumeName]
    if option:
        command.append(option)
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeResetFailedException(rc=e.rc, err=e.err)


@makePublic
def volumeAddBrick(volumeName, brickList,
                   replicaCount=0, stripeCount=0, force=False):
    command = _getGlusterVolCmd() + ["add-brick", volumeName]
    if stripeCount:
        command += ["stripe", "%s" % stripeCount]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    command += brickList
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeBrickAddFailedException(rc=e.rc, err=e.err)


@makePublic
def volumeRebalanceStart(volumeName, rebalanceType="", force=False):
    command = _getGlusterVolCmd() + ["rebalance", volumeName]
    if rebalanceType:
        command.append(rebalanceType)
    command.append("start")
    if force:
        command.append("force")
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRebalanceStartFailedException(rc=e.rc,
                                                            err=e.err)
    try:
        return {'taskId': xmltree.find('volRebalance/task-id').text}
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeRebalanceStop(volumeName, force=False):
    command = _getGlusterVolCmd() + ["rebalance", volumeName, "stop"]
    if force:
        command.append('force')
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRebalanceStopFailedException(rc=e.rc,
                                                           err=e.err)

    try:
        return _parseVolumeRebalanceRemoveBrickStatus(xmltree, 'rebalance')
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def _parseVolumeRebalanceRemoveBrickStatus(xmltree, mode):
    """
    returns {'hosts': [{'name': NAME,
                        'id': UUID_STRING,
                        'runtime': FLOAT_AS_STRING,
                        'filesScanned': INT AS STRING,
                        'filesMoved': INT AS STRING,
                        'filesFailed': INT AS STRING,
                        'filesSkipped': INT AS STRING,
                        'totalSizeMoved': INT AS STRING,
                        'status': STRING},...]
             'summary': {'runtime': FLOAT_AS_STRING,
                         'filesScanned': INT AS STRING,
                         'filesMoved': INT AS STRING,
                         'filesFailed': INT AS STRING,
                         'filesSkipped': INT AS STRING,
                         'totalSizeMoved': INT AS STRING,
                         'status': STRING}}
    """
    if mode == 'rebalance':
        tree = xmltree.find('volRebalance')
    elif mode == 'remove-brick':
        tree = xmltree.find('volRemoveBrick')
    else:
        return

    st = tree.find('aggregate/statusStr').text
    statusStr = st.replace(' ', '_').replace('-', '_')
    status = {
        'summary': {
            'runtime': tree.find('aggregate/runtime').text,
            'filesScanned': tree.find('aggregate/lookups').text,
            'filesMoved': tree.find('aggregate/files').text,
            'filesFailed': tree.find('aggregate/failures').text,
            'filesSkipped': tree.find('aggregate/skipped').text,
            'totalSizeMoved': tree.find('aggregate/size').text,
            'status': statusStr.upper()},
        'hosts': []}

    for el in tree.findall('node'):
        st = el.find('statusStr').text
        statusStr = st.replace(' ', '_').replace('-', '_')
        status['hosts'].append({'name': el.find('nodeName').text,
                                'id': el.find('id').text,
                                'runtime': el.find('runtime').text,
                                'filesScanned': el.find('lookups').text,
                                'filesMoved': el.find('files').text,
                                'filesFailed': el.find('failures').text,
                                'filesSkipped': el.find('skipped').text,
                                'totalSizeMoved': el.find('size').text,
                                'status': statusStr.upper()})

    return status


@makePublic
def volumeRebalanceStatus(volumeName):
    command = _getGlusterVolCmd() + ["rebalance", volumeName, "status"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRebalanceStatusFailedException(rc=e.rc,
                                                             err=e.err)
    try:
        return _parseVolumeRebalanceRemoveBrickStatus(xmltree, 'rebalance')
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeReplaceBrickCommitForce(volumeName, existingBrick, newBrick):
    command = _getGlusterVolCmd() + ["replace-brick", volumeName,
                                     existingBrick, newBrick, "commit",
                                     "force"]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeReplaceBrickCommitForceFailedException(rc=e.rc,
                                                                     err=e.err)


@makePublic
def volumeRemoveBrickStart(volumeName, brickList, replicaCount=0):
    command = _getGlusterVolCmd() + ["remove-brick", volumeName]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    command += brickList + ["start"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRemoveBrickStartFailedException(rc=e.rc,
                                                              err=e.err)
    try:
        return {'taskId': xmltree.find('volRemoveBrick/task-id').text}
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeRemoveBrickStop(volumeName, brickList, replicaCount=0):
    command = _getGlusterVolCmd() + ["remove-brick", volumeName]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    command += brickList + ["stop"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRemoveBrickStopFailedException(rc=e.rc,
                                                             err=e.err)

    try:
        return _parseVolumeRebalanceRemoveBrickStatus(xmltree, 'remove-brick')
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeRemoveBrickStatus(volumeName, brickList, replicaCount=0):
    command = _getGlusterVolCmd() + ["remove-brick", volumeName]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    command += brickList + ["status"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRemoveBrickStatusFailedException(rc=e.rc,
                                                               err=e.err)
    try:
        return _parseVolumeRebalanceRemoveBrickStatus(xmltree, 'remove-brick')
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeRemoveBrickCommit(volumeName, brickList, replicaCount=0):
    command = _getGlusterVolCmd() + ["remove-brick", volumeName]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    command += brickList + ["commit"]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRemoveBrickCommitFailedException(rc=e.rc,
                                                               err=e.err)


@makePublic
def volumeRemoveBrickForce(volumeName, brickList, replicaCount=0):
    command = _getGlusterVolCmd() + ["remove-brick", volumeName]
    if replicaCount:
        command += ["replica", "%s" % replicaCount]
    command += brickList + ["force"]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeRemoveBrickForceFailedException(rc=e.rc,
                                                              err=e.err)


@makePublic
def peerProbe(hostName):
    command = _getGlusterPeerCmd() + ["probe", hostName]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterHostAddFailedException(rc=e.rc, err=e.err)


@makePublic
def peerDetach(hostName, force=False):
    command = _getGlusterPeerCmd() + ["detach", hostName]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        if e.rc == 2:
            raise ge.GlusterHostNotFoundException(rc=e.rc, err=e.err)
        else:
            raise ge.GlusterHostRemoveFailedException(rc=e.rc, err=e.err)


def _parsePeerStatus(tree, gHostName, gUuid, gStatus):
    hostList = [{'hostname': gHostName,
                 'uuid': gUuid,
                 'status': gStatus}]

    for el in tree.findall('peerStatus/peer'):
        if el.find('state').text != '3':
            status = HostStatus.UNKNOWN
        elif el.find('connected').text == '1':
            status = HostStatus.CONNECTED
        else:
            status = HostStatus.DISCONNECTED
        hostList.append({'hostname': el.find('hostname').text,
                         'uuid': el.find('uuid').text,
                         'status': status})

    return hostList


@makePublic
def peerStatus():
    """
    Returns:
        [{'hostname': HOSTNAME, 'uuid': UUID, 'status': STATE}, ...]
    """
    command = _getGlusterPeerCmd() + ["status"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterHostsListFailedException(rc=e.rc, err=e.err)
    try:
        return _parsePeerStatus(xmltree,
                                _getLocalIpAddress() or _getGlusterHostName(),
                                hostUUIDGet(), HostStatus.CONNECTED)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeProfileStart(volumeName):
    command = _getGlusterVolCmd() + ["profile", volumeName, "start"]
    try:
        _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeProfileStartFailedException(rc=e.rc, err=e.err)
    return True


@makePublic
def volumeProfileStop(volumeName):
    command = _getGlusterVolCmd() + ["profile", volumeName, "stop"]
    try:
        _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeProfileStopFailedException(rc=e.rc, err=e.err)
    return True


@makePublic
def volumeProfileInfo(volumeName, nfs=False):
    """
    Returns:
    When nfs=True:
    {'volumeName': VOLUME-NAME,
     'nfsServers': [
         {'nfs': SERVER-NAME,
          'cumulativeStats': {'blockStats': [{'size': int,
                                              'read': int,
                                              'write': int}, ...],
                              'fopStats': [{'name': FOP-NAME,
                                            'hits': int,
                                            'latencyAvg': float,
                                            'latencyMin': float,
                                            'latencyMax': float}, ...],
                              'duration': int,
                              'totalRead': int,
                              'totalWrite': int},
          'intervalStats': {'blockStats': [{'size': int,
                                            'read': int,
                                            'write': int}, ...],
                            'fopStats': [{'name': FOP-NAME,
                                          'hits': int,
                                          'latencyAvg': float,
                                          'latencyMin': float,
                                          'latencyMax': float}, ...],
                            'duration': int,
                            'totalRead': int,
                            'totalWrite': int}}, ...]}

    When nfs=False:
    {'volumeName': VOLUME-NAME,
     'bricks': [
         {'brick': BRICK-NAME,
          'cumulativeStats': {'blockStats': [{'size': int,
                                              'read': int,
                                              'write': int}, ...],
                              'fopStats': [{'name': FOP-NAME,
                                            'hits': int,
                                            'latencyAvg': float,
                                            'latencyMin': float,
                                            'latencyMax': float}, ...],
                              'duration': int,
                              'totalRead': int,
                              'totalWrite': int},
          'intervalStats': {'blockStats': [{'size': int,
                                            'read': int,
                                            'write': int}, ...],
                            'fopStats': [{'name': FOP-NAME,
                                          'hits': int,
                                          'latencyAvg': float,
                                          'latencyMin': float,
                                          'latencyMax': float}, ...],
                            'duration': int,
                            'totalRead': int,
                            'totalWrite': int}}, ...]}
    """
    command = _getGlusterVolCmd() + ["profile", volumeName, "info"]
    if nfs:
        command += ["nfs"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeProfileInfoFailedException(rc=e.rc, err=e.err)
    try:
        return _parseVolumeProfileInfo(xmltree, nfs)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


def _parseVolumeTasks(tree):
    """
    returns {TaskId: {'volumeName': VolumeName,
                      'taskType': TaskType,
                      'status': STATUS,
                      'bricks': BrickList}, ...}
    """
    tasks = {}
    for el in tree.findall('volStatus/volumes/volume'):
        volumeName = el.find('volName').text
        for c in el.findall('tasks/task'):
            taskType = c.find('type').text
            taskType = taskType.upper().replace('-', '_').replace(' ', '_')
            taskId = c.find('id').text
            bricks = []
            if taskType == TaskType.REPLACE_BRICK:
                bricks.append(c.find('params/srcBrick').text)
                bricks.append(c.find('params/dstBrick').text)
            elif taskType == TaskType.REMOVE_BRICK:
                for b in c.findall('params/brick'):
                    bricks.append(b.text)
            elif taskType == TaskType.REBALANCE:
                pass

            statusStr = c.find('statusStr').text.upper() \
                                                .replace('-', '_') \
                                                .replace(' ', '_')

            tasks[taskId] = {'volumeName': volumeName,
                             'taskType': taskType,
                             'status': statusStr,
                             'bricks': bricks}
    return tasks


@makePublic
def volumeTasks(volumeName="all"):
    command = _getGlusterVolCmd() + ["status", volumeName, "tasks"]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeTasksFailedException(rc=e.rc, err=e.err)
    try:
        return _parseVolumeTasks(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeGeoRepSessionStart(volumeName, remoteHost, remoteVolumeName,
                             remoteUserName=None, force=False):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolGeoRepCmd() + [volumeName, "%s::%s" % (
        userAtHost, remoteVolumeName), "start"]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeGeoRepSessionStartFailedException(rc=e.rc,
                                                                err=e.err)


@makePublic
def volumeGeoRepSessionStop(volumeName, remoteHost, remoteVolumeName,
                            remoteUserName=None, force=False):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolGeoRepCmd() + [volumeName, "%s::%s" % (
        userAtHost, remoteVolumeName), "stop"]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeGeoRepSessionStopFailedException(rc=e.rc,
                                                               err=e.err)


def _parseGeoRepStatus(tree):
    """
    Returns:
    {volume-name: [{sessionKey: 'key to identify the session',
                    remoteVolumeName: 'volume in remote gluster cluster'
                    bricks: [{host: 'local node',
                              hostUuid: 'uuid of brick host',
                              brickName: 'brick in the local volume',
                              remoteHost: 'slave',
                              status: 'status'
                              remoteUserName: 'root'
                              timeZone: 'nodes time zone'
                              crawlStatus: 'crawlStatus'
                              lastSynced: 'last synced time'
                              entry: 'nos of entry operations pending'
                              data: 'nos of data operations pending'
                              meta: 'nos of meta operations pending'
                              failures: 'nos of failures'
                              checkpointTime: 'checkpoint set time'
                              checkpointCompletionTime: 'checkpoint completion
                                                         time'
                              checkpointCompleted: 'yes/no'}]...
               ]....
    }
    """
    status = {}
    for volume in tree.findall('geoRep/volume'):
        sessions = []
        volumeDetail = {}
        for session in volume.findall('sessions/session'):
            pairs = []
            sessionDetail = {}
            sessionDetail['sessionKey'] = session.find('session_slave').text
            sessionDetail['remoteVolumeName'] = sessionDetail[
                'sessionKey'].split("::")[-1]
            for pair in session.findall('pair'):
                pairDetail = {}
                pairDetail['host'] = pair.find('master_node').text
                pairDetail['hostUuid'] = pair.find(
                    'master_node_uuid').text
                pairDetail['brickName'] = pair.find('master_brick').text
                pairDetail['remoteHost'] = pair.find('slave_node').text
                pairDetail['remoteUserName'] = pair.find('slave_user').text
                pairDetail['status'] = pair.find('status').text
                pairDetail['crawlStatus'] = pair.find('crawl_status').text
                pairDetail['timeZone'] = _TIME_ZONE
                pairDetail['lastSynced'] = pair.find('last_synced').text
                if pairDetail['lastSynced'] != 'N/A':
                    pairDetail['lastSynced'] = calendar.timegm(
                        time.strptime(pairDetail['lastSynced'],
                                      "%Y-%m-%d %H:%M:%S"))

                pairDetail['checkpointTime'] = pair.find(
                    'checkpoint_time').text
                if pairDetail['checkpointTime'] != 'N/A':
                    pairDetail['checkpointTime'] = calendar.timegm(
                        time.strptime(pairDetail['checkpointTime'],
                                      "%Y-%m-%d %H:%M:%S"))

                pairDetail['checkpointCompletionTime'] = pair.find(
                    'checkpoint_completion_time').text
                if pairDetail['checkpointCompletionTime'] != 'N/A':
                    pairDetail['checkpointCompletionTime'] = calendar.timegm(
                        time.strptime(pairDetail['checkpointCompletionTime'],
                                      "%Y-%m-%d %H:%M:%S"))

                pairDetail['entry'] = pair.find('entry').text
                pairDetail['data'] = pair.find('data').text
                pairDetail['meta'] = pair.find('meta').text
                pairDetail['failures'] = pair.find('failures').text
                pairDetail['checkpointCompleted'] = pair.find(
                    'checkpoint_completed').text
                pairs.append(pairDetail)
            sessionDetail['bricks'] = pairs
            sessions.append(sessionDetail)
        volumeDetail['sessions'] = sessions
        status[volume.find('name').text] = volumeDetail
    return status


@makePublic
def volumeGeoRepStatus(volumeName=None, remoteHost=None,
                       remoteVolumeName=None, remoteUserName=None):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolGeoRepCmd()
    if volumeName:
        command.append(volumeName)
    if remoteHost and remoteVolumeName:
        command.append("%s::%s" % (userAtHost, remoteVolumeName))
    command.append("status")

    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterGeoRepStatusFailedException(rc=e.rc, err=e.err)
    try:
        return _parseGeoRepStatus(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def volumeGeoRepSessionPause(volumeName, remoteHost, remoteVolumeName,
                             remoteUserName=None, force=False):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolGeoRepCmd() + [volumeName, "%s::%s" % (
        userAtHost, remoteVolumeName), "pause"]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeGeoRepSessionPauseFailedException(rc=e.rc,
                                                                err=e.err)


@makePublic
def volumeGeoRepSessionResume(volumeName, remoteHost, remoteVolumeName,
                              remoteUserName=None, force=False):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolGeoRepCmd() + [volumeName, "%s::%s" % (
        userAtHost, remoteVolumeName), "resume"]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterVolumeGeoRepSessionResumeFailedException(rc=e.rc,
                                                                 err=e.err)


def _parseVolumeGeoRepConfig(tree):
    """
    Returns:
    {geoRepConfig:{'optionName': 'optionValue',...}
    }
    """
    conf = tree.find('geoRep/config')
    config = {}
    for child in conf.getchildren():
        config[child.tag] = child.text
    return {'geoRepConfig': config}


@makePublic
def volumeGeoRepConfig(volumeName, remoteHost,
                       remoteVolumeName, optionName=None,
                       optionValue=None,
                       remoteUserName=None):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolGeoRepCmd() + [volumeName, "%s::%s" % (
        userAtHost, remoteVolumeName), "config"]
    if optionName and optionValue:
        command += [optionName, optionValue]
    elif optionName:
        command += ["!%s" % optionName]

    try:
        xmltree = _execGlusterXml(command)
        if optionName:
            return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterGeoRepConfigFailedException(rc=e.rc, err=e.err)
    try:
        return _parseVolumeGeoRepConfig(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def snapshotCreate(volumeName, snapName,
                   snapDescription=None,
                   force=False):
    command = _getGlusterSnapshotCmd() + ["create", snapName, volumeName]

    if snapDescription:
        command += ['description', snapDescription]
    if force:
        command.append('force')

    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterSnapshotCreateFailedException(rc=e.rc, err=e.err)
    try:
        return {'uuid': xmltree.find('snapCreate/snapshot/uuid').text,
                'name': xmltree.find('snapCreate/snapshot/name').text}
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


@makePublic
def snapshotDelete(volumeName=None, snapName=None):
    command = _getGlusterSnapshotCmd() + ["delete"]
    if snapName:
        command.append(snapName)
    elif volumeName:
        command += ["volume", volumeName]

    # xml output not used because of BZ:1161416 in gluster cli
    rc, out, err = _execGluster(command)
    if rc:
        raise ge.GlusterSnapshotDeleteFailedException(rc, out, err)
    else:
        return True


@makePublic
def snapshotActivate(snapName, force=False):
    command = _getGlusterSnapshotCmd() + ["activate", snapName]

    if force:
        command.append('force')

    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterSnapshotActivateFailedException(rc=e.rc, err=e.err)


@makePublic
def snapshotDeactivate(snapName):
    command = _getGlusterSnapshotCmd() + ["deactivate", snapName]

    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterSnapshotDeactivateFailedException(rc=e.rc, err=e.err)


def _parseRestoredSnapshot(tree):
    """
    returns {'volumeName': 'vol1',
             'volumeUuid': 'uuid',
             'snapshotName': 'snap2',
             'snapshotUuid': 'uuid'
            }
    """
    snapshotRestore = {}
    snapshotRestore['volumeName'] = tree.find('snapRestore/volume/name').text
    snapshotRestore['volumeUuid'] = tree.find('snapRestore/volume/uuid').text
    snapshotRestore['snapshotName'] = tree.find(
        'snapRestore/snapshot/name').text
    snapshotRestore['snapshotUuid'] = tree.find(
        'snapRestore/snapshot/uuid').text

    return snapshotRestore


@makePublic
def snapshotRestore(snapName):
    command = _getGlusterSnapshotCmd() + ["restore", snapName]

    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterSnapshotRestoreFailedException(rc=e.rc, err=e.err)
    try:
        return _parseRestoredSnapshot(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


def _parseSnapshotConfigList(tree):
    """
    returns {'system':{'snap-max-hard-limit': 'hardlimit',
                       'snap-max-soft-limit': 'softLimit',
                       'auto-delete': 'enable/disable',
                       'activate-on-create': 'enable/disable'},
             'volume':{'name' :
                          {'snap-max-hard-limit: 'hardlimit'}
                      }
            }
    """
    systemConfig = {}
    systemConfig['snap-max-hard-limit'] = tree.find(
        'snapConfig/systemConfig/hardLimit').text
    systemConfig['snap-max-soft-limit'] = tree.find(
        'snapConfig/systemConfig/softLimit').text
    systemConfig['auto-delete'] = tree.find(
        'snapConfig/systemConfig/autoDelete').text
    systemConfig['activate-on-create'] = tree.find(
        'snapConfig/systemConfig/activateOnCreate').text

    volumeConfig = {}
    for el in tree.findall('snapConfig/volumeConfig/volume'):
        config = {}
        volumeName = el.find('name').text
        config['snap-max-hard-limit'] = el.find('effectiveHardLimit').text
        volumeConfig[volumeName] = config

    return {'system': systemConfig, 'volume': volumeConfig}


@makePublic
def snapshotConfig(volumeName=None, optionName=None, optionValue=None):
    command = _getGlusterSnapshotCmd() + ["config"]
    if volumeName:
        command.append(volumeName)
    if optionName and optionValue:
        command += [optionName, optionValue]

    try:
        xmltree = _execGlusterXml(command)
        if optionName and optionValue:
            return
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterSnapshotConfigFailedException(rc=e.rc, err=e.err)
    try:
        return _parseSnapshotConfigList(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorException(err=[etree.tostring(xmltree)])


def _parseVolumeSnapshotList(tree):
    """
    {'v1': {'snapshots': [{'name': 'snap1_v1',
                           'description': description of the snapshot,
                           'id': '8add41ae-c60c-4023'
                                           '-a1a6-5093a5d35603',
                           'createTime': {'timeZone': 'IST',
                                          'epochTime': 1414427114}
                           'snapVolume': '5eeaf23def3f446d898e1de8461a6aa7'
                           'snapVolumeStatus': 'ACTIVATED'}, ...],
            'snapRemaining': 252}
    }
    """
    volume = {}
    volumeName = tree.find(
        'snapInfo/originVolume/name').text
    volume[volumeName] = {
        'snapRemaining': tree.find('snapInfo/originVolume/snapRemaining').text,
        'snapshots': []
    }
    if int(tree.find('snapInfo/count').text) == 0:
        return {}
    for el in tree.findall('snapInfo/snapshots/snapshot'):
        snapshot = {}
        snapshot['id'] = el.find('uuid').text
        snapshot['description'] = "" if el.find('description') is None \
                                  else el.find('description').text
        snapshot['createTime'] = {
            'epochTime': calendar.timegm(
                time.strptime(el.find('createTime').text,
                              "%Y-%m-%d %H:%M:%S")
            ),
            'timeZone': _TIME_ZONE
        }
        snapshot['snapVolume'] = el.find('snapVolume/name').text
        status = el.find('snapVolume/status').text
        if status.upper() == 'STARTED':
            snapshot['snapVolumeStatus'] = SnapshotStatus.ACTIVATED
        else:
            snapshot['snapVolumeStatus'] = SnapshotStatus.DEACTIVATED
        snapshot['name'] = el.find('name').text
        volume[volumeName]['snapshots'].append(snapshot)
    return volume


def _parseAllVolumeSnapshotList(tree):
    """
    {'v1': {'snapshots': [{'name': 'snap1_v1',
                           'description': description of the snapshot,
                           'id': '8add41ae-c60c-4023-'
                                           'a1a6-5093a5d35603',
                           'createTime': {'timeZone': 'IST',
                                          'epochTime': 141442711}
                           'snapVolume': '5eeaf23def3f446d898e1de8461a6aa7'
                           'snapVolumeStatus': 'ACTIVATED'}, ...],
            'snapRemaining': 252},
     'v2': {'snapshots': [{'name': 'snap1_v2',
                           'description': description of the snapshot,
                           'id': '8add41ae-c60c-4023'
                                           '-a1a6-1233a5d35603',
                           'createTime': {'timeZone': 'IST',
                                          'epochTime': 1414427114}
                           'snapVolume': '5eeaf23def3f446d898e1123461a6aa7'
                           'snapVolumeStatus': 'DEACTIVATED'}, ...],
            'snapRemaining': 252},...
    }
    """
    volumes = {}
    if int(tree.find('snapInfo/count').text) == 0:
        return {}
    for el in tree.findall('snapInfo/snapshots/snapshot'):
        snapshot = {}
        snapshot['id'] = el.find('uuid').text
        snapshot['description'] = "" if el.find('description') is None \
                                  else el.find('description').text
        snapshot['createTime'] = {
            'epochTime': calendar.timegm(
                time.strptime(el.find('createTime').text,
                              "%Y-%m-%d %H:%M:%S")
            ),
            'timeZone': _TIME_ZONE
        }
        snapshot['snapVolumeName'] = el.find('snapVolume/name').text
        status = el.find('snapVolume/status').text
        if status.upper() == 'STARTED':
            snapshot['snapVolumeStatus'] = SnapshotStatus.ACTIVATED
        else:
            snapshot['snapVolumeStatus'] = SnapshotStatus.DEACTIVATED
        snapshot['name'] = el.find('name').text
        volumeName = el.find('snapVolume/originVolume/name').text
        if volumeName not in volumes:
            volumes[volumeName] = {
                'snapRemaining': el.find(
                    'snapVolume/originVolume/snapRemaining').text,
                'snapshots': []
            }
        volumes[volumeName]['snapshots'].append(snapshot)
    return volumes


@makePublic
def snapshotInfo(volumeName=None):
    command = _getGlusterSnapshotCmd() + ["info"]
    if volumeName:
        command += ["volume", volumeName]
    try:
        xmltree = _execGlusterXml(command)
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterSnapshotInfoFailedException(rc=e.rc, err=e.err)
    try:
        if volumeName:
            return _parseVolumeSnapshotList(xmltree)
        else:
            return _parseAllVolumeSnapshotList(xmltree)
    except _etreeExceptions:
        raise ge.GlusterXmlErrorInfoException(err=[etree.tostring(xmltree)])


@makePublic
def executeGsecCreate():
    command = _getGlusterSystemCmd() + ["execute", "gsec_create"]
    rc, out, err = _execGluster(command)
    if rc:
        raise ge.GlusterGeoRepPublicKeyFileCreateFailedException(rc,
                                                                 out, err)
    return True


@makePublic
def executeMountBrokerUserAdd(remoteUserName, remoteVolumeName):
    command = _getGlusterSystemCmd() + ["execute", "mountbroker",
                                        "user", remoteUserName,
                                        remoteVolumeName]
    rc, out, err = _execGluster(command)
    if rc:
        raise ge.GlusterGeoRepExecuteMountBrokerUserAddFailedException(rc,
                                                                       out,
                                                                       err)
    return True


@makePublic
def executeMountBrokerOpt(optionName, optionValue):
    command = _getGlusterSystemCmd() + ["execute", "mountbroker",
                                        "opt", optionName,
                                        optionValue]
    rc, out, err = _execGluster(command)
    if rc:
        raise ge.GlusterGeoRepExecuteMountBrokerOptFailedException(rc,
                                                                   out, err)
    return True


@makePublic
def volumeGeoRepSessionCreate(volumeName, remoteHost,
                              remoteVolumeName,
                              remoteUserName=None, force=False):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolCmd() + ["geo-replication", volumeName,
                                     "%s::%s" % (userAtHost, remoteVolumeName),
                                     "create", "no-verify"]
    if force:
        command.append('force')
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterGeoRepSessionCreateFailedException(rc=e.rc, err=e.err)


@makePublic
def volumeGeoRepSessionDelete(volumeName, remoteHost, remoteVolumeName,
                              remoteUserName=None):
    if remoteUserName:
        userAtHost = "%s@%s" % (remoteUserName, remoteHost)
    else:
        userAtHost = remoteHost
    command = _getGlusterVolCmd() + ["geo-replication", volumeName,
                                     "%s::%s" % (userAtHost, remoteVolumeName),
                                     "delete"]
    try:
        _execGlusterXml(command)
        return True
    except ge.GlusterCmdFailedException as e:
        raise ge.GlusterGeoRepSessionDeleteFailedException(rc=e.rc, err=e.err)
