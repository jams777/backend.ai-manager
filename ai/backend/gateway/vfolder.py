import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

import aiohttp
from aiohttp import web
import aiotools
import sqlalchemy as sa
import psycopg2

from .auth import auth_required
from .exceptions import FolderNotFound, FolderAlreadyExists, InvalidAPIParameters
from ..manager.models import (keypairs, vfolders, vfolder_invitations,
                              vfolder_permissions)

log = logging.getLogger('ai.backend.gateway.vfolder')

_rx_slug = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$')


@auth_required
async def create(request):
    resp = {}
    dbpool = request.app['dbpool']
    access_key = request['keypair']['access_key']
    params = await request.json()
    log.info(f"VFOLDER.CREATE (u:{access_key})")
    assert _rx_slug.search(params['name']) is not None
    async with dbpool.acquire() as conn:
        # Prevent creation of vfolder with duplicated name.
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           (vfolder_permissions.c.access_key == access_key)) &
                          (vfolders.c.name == params['name'])))
        result = await conn.execute(query)
        if result.rowcount > 0:
            raise FolderAlreadyExists

        folder_id = uuid.uuid4().hex
        # TODO: make this configurable
        folder_host = 'azure-shard01'
        folder_path = (request.app['VFOLDER_MOUNT'] / folder_host / folder_id)
        folder_path.mkdir(parents=True, exist_ok=True)
        query = (vfolders.insert().values({
            'id': folder_id,
            'name': params['name'],
            'host': folder_host,
            'last_used': None,
            'belongs_to': access_key,
        }))
        resp = {
            'id': folder_id,
            'name': params['name'],
        }
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        assert result.rowcount == 1
    return web.json_response(resp, status=201)


@auth_required
async def list_folders(request):
    resp = []
    dbpool = request.app['dbpool']
    access_key = request['keypair']['access_key']
    log.info(f"VFOLDER.LIST (u:{access_key})")
    async with dbpool.acquire() as conn:
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           (vfolder_permissions.c.access_key == access_key))))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        async for row in result:
            resp.append({
                'name': row.name,
                'id': row.id.hex,
                'is_owner': False if row.permission else True,
                'permission': row.permission if row.permission else 'rw',
            })
    return web.json_response(resp, status=200)


@auth_required
async def get_info(request):
    resp = {}
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    log.info(f"VFOLDER.GETINFO (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn:
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           (vfolder_permissions.c.access_key == access_key)) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            raise FolderNotFound()
        is_owner = False if row.permission else True
        permission = row.permission if row.permission else 'rw'
        # TODO: handle nested directory structure
        folder_path = (request.app['VFOLDER_MOUNT'] / row.host / row.id.hex)
        num_files = len(list(folder_path.iterdir()))
        resp = {
            'name': row.name,
            'id': row.id.hex,
            'numFiles': num_files,
            'created': str(row.created_at),
            'is_owner': is_owner,
            'permission': permission,
        }
    return web.json_response(resp, status=200)


@auth_required
async def upload(request):
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    log.info(f"VFOLDER.UPLOAD (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn:
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           ((vfolder_permissions.c.access_key == access_key) &
                            (vfolder_permissions.c.permission == 'rw'))) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            log.error('why here')
            raise FolderNotFound()
        folder_path = (request.app['VFOLDER_MOUNT'] / row.host / row.id.hex)
        reader = await request.multipart()
        file_count = 0
        async for file in aiotools.aiter(reader.next, None):
            # TODO: impose limits on file size and count
            file_count += 1
            file_dir = (folder_path / file.filename).parent
            file_dir.mkdir(parents=True, exist_ok=True)
            with open(folder_path / file.filename, 'wb') as f:
                while not file.at_eof():
                    chunk = await file.read_chunk(size=8192)
                    f.write(file.decode(chunk))
    return web.Response(status=201)


@auth_required
async def delete_files(request):
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    params = await request.json()
    assert params.get('files'), 'no file(s) specified!'
    files = params.get('files')
    log.info(f"VFOLDER.DELETE_FILES (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn:
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           ((vfolder_permissions.c.access_key == access_key) &
                            (vfolder_permissions.c.permission == 'rw'))) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            raise FolderNotFound()
        folder_path = (request.app['VFOLDER_MOUNT'] / row.host / row.id.hex)
        for file in files:
            file_path = folder_path / file
            if file_path.is_dir():
                shutil.rmtree(file_path)
            elif file_path.is_file():
                os.unlink(file_path)
    resp = {}
    return web.json_response(resp, status=200)


@auth_required
async def download(request):
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    params = await request.json()
    assert params.get('files'), 'no file(s) specified!'
    files = params.get('files')
    log.info(f"VFOLDER.DOWNLOAD (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn:
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           (vfolder_permissions.c.access_key == access_key)) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            raise FolderNotFound()
        folder_path = (request.app['VFOLDER_MOUNT'] / row.host / row.id.hex)
        with aiohttp.MultipartWriter('mixed') as mpwriter:
            total_payloads_length = 0
            headers = {'Content-Encoding': 'gzip'}
            try:
                for file in files:
                    data = open(folder_path / file, 'rb')
                    payload = mpwriter.append(data, headers)
                    total_payloads_length += payload.size
            except FileNotFoundError:
                return web.Response(status=404, reason='File not found')
            mpwriter._headers['X-TOTAL-PAYLOADS-LENGTH'] = str(total_payloads_length)
            return web.Response(body=mpwriter, status=200)


@auth_required
async def list_files(request):
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    params = await request.json()
    log.info(f"VFOLDER.LIST_FILES (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn:
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == access_key) |
                           (vfolder_permissions.c.access_key == access_key)) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            raise FolderNotFound()
        base_path = (request.app['VFOLDER_MOUNT'] / row.host / row.id.hex)
        folder_path = base_path / params['path'] if 'path' in params else base_path
        if not str(folder_path).startswith(str(base_path)):
            resp = {'error_msg': 'No such file or directory'}
            return web.json_response(resp, status=404)
        files = []
        for f in os.scandir(folder_path):
            fstat = f.stat()
            ctime = fstat.st_ctime  # TODO: way to get concrete create time?
            mtime = fstat.st_mtime
            atime = fstat.st_atime
            files.append({
                'mode': stat.filemode(fstat.st_mode),
                'size': fstat.st_size,
                'ctime': ctime,
                'mtime': mtime,
                'atime': atime,
                'filename': f.name,
            })
        resp = {
            'files': json.dumps(files),
            'folder_path': str(folder_path),
        }
    return web.json_response(resp, status=200)


@auth_required
async def invite(request):
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    params = await request.json()
    perm = params.get('perm', 'rw')
    user_ids = params.get('user_ids', [])
    assert perm in ['ro', 'rw']
    assert len(user_ids) > 0, 'no user ids'
    log.info(f"VFOLDER.INVITE (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn:
        # Get virtual folder.
        query = (sa.select('*')
                   .select_from(vfolders)
                   .where((vfolders.c.belongs_to == access_key) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        vf = await result.first()
        if vf is None:
            raise FolderNotFound()

        # Get invited user's keypairs except vfolder owner.
        query = (sa.select('*')
                   .select_from(keypairs)
                   .where(keypairs.c.user_id.in_(user_ids))
                   .where(keypairs.c.user_id != request['user']['id']))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        kps = await result.fetchall()

        # Create invitation.
        invitees = [kp.user_id for kp in kps]
        invited_ids = []
        for invitee in set(invitees):
            inviter = request['user']['id']
            # Do not create invitation if already exists.
            query = (sa.select('*')
                       .select_from(vfolder_invitations)
                       .where((vfolder_invitations.c.inviter == inviter) &
                              (vfolder_invitations.c.invitee == invitee) &
                              (vfolder_invitations.c.vfolder == vf.id) &
                              (vfolder_invitations.c.state == 'pending')))
            result = await conn.execute(query)
            if result.rowcount > 0:
                continue

            # TODO: insert multiple values with one query.
            #       insert().values([{}, {}, ...]) does not work:
            #       sqlalchemy.exc.CompileError: The 'default' dialect with current
            #       database version settings does not support in-place multirow
            #       inserts.
            query = (vfolder_invitations.insert().values({
                'id': uuid.uuid4().hex,
                'permission': perm,
                'vfolder': vf.id,
                'inviter': inviter,
                'invitee': invitee,
                'state': 'pending',
            }))
            try:
                await conn.execute(query)
                invited_ids.append(invitee)
            except psycopg2.DataError as e:
                pass
    resp = {'invited_ids': invited_ids}
    return web.json_response(resp, status=201)


@auth_required
async def invitations(request):
    dbpool = request.app['dbpool']
    access_key = request['keypair']['access_key']
    log.info(f"VFOLDER.INVITATION (u:{access_key})")
    async with dbpool.acquire() as conn:
        query = (sa.select('*')
                   .select_from(vfolder_invitations)
                   .where((vfolder_invitations.c.invitee == request['user']['id']) &
                          (vfolder_invitations.c.state == 'pending')))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        invitations = await result.fetchall()
    invs_info = []
    for inv in invitations:
        invs_info.append({
            'id': str(inv.id),
            'inviter': inv.inviter,
            'perm': inv.permission,
            'state': inv.state,
            'created_at': str(inv.created_at),
            'vfolder_id': str(inv.vfolder),
        })
    resp = {'invitations': invs_info}
    return web.json_response(resp, status=200)


@auth_required
async def accept_invitation(request):
    dbpool = request.app['dbpool']
    access_key = request['keypair']['access_key']
    params = await request.json()
    inv_id = params['inv_id']
    inv_ak = params['inv_ak']
    log.info(f"VFOLDER.ACCEPT_INVITATION (u:{access_key})")
    async with dbpool.acquire() as conn:
        # Get invitation.
        query = (sa.select('*')
                   .select_from(vfolder_invitations)
                   .where((vfolder_invitations.c.id == inv_id) &
                          (vfolder_invitations.c.state == 'pending')))
        result = await conn.execute(query)
        invitation = await result.first()

        # Get target virtual folder.
        query = (sa.select('*')
                   .select_from(vfolders)
                   .where(vfolders.c.id == invitation.vfolder))
        result = await conn.execute(query)
        target_vfolder = await result.first()
        if target_vfolder is None:
            resp = {'msg': 'No such virtual folder found.'}
            return web.json_response(resp, status=404)

        # Prevent accepting vfolder with duplicated name.
        j = sa.join(vfolders, vfolder_permissions,
                    vfolders.c.id == vfolder_permissions.c.vfolder, isouter=True)
        query = (sa.select('*')
                   .select_from(j)
                   .where(((vfolders.c.belongs_to == inv_ak) |
                           (vfolder_permissions.c.access_key == inv_ak)) &
                          (vfolders.c.name == target_vfolder.name)))
        result = await conn.execute(query)
        if result.rowcount > 0:
            raise FolderAlreadyExists

        if invitation is None:
            resp = {'msg': 'No such invitation found.'}
            return web.json_response(resp, status=404)

        # Check the access_key is valid by comparing with invitation info.
        query = (sa.select('*')
                   .select_from(keypairs)
                   .where(keypairs.c.access_key == inv_ak))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            invitation = {'msg': 'Invalid invitee access_key'}
            return web.json_response(resp, status=404)
        assert row.user_id == invitation.invitee

        # Create permission relation between the vfolder and the invitee.
        query = (vfolder_permissions.insert().values({
            'permission': invitation.permission,
            'vfolder': invitation.vfolder,
            'access_key': inv_ak,
        }))
        await conn.execute(query)

        # Clear used invitation.
        query = (vfolder_invitations.update()
                                    .where(vfolder_invitations.c.id == inv_id)
                                    .values(state='accepted'))
        await conn.execute(query)
    msg = (f'Access key ({inv_ak} by {invitation.invitee}) now can access '
           f'vfolder {invitation.vfolder}.')
    return web.json_response({'msg': msg}, status=201)


@auth_required
async def delete_invitation(request):
    dbpool = request.app['dbpool']
    access_key = request['keypair']['access_key']
    params = await request.json()
    inv_id = params['inv_id']
    log.info(f"VFOLDER.DELETE_INVITATION (u:{access_key})")
    async with dbpool.acquire() as conn:
        query = (sa.select('*')
                   .select_from(vfolder_invitations)
                   .where((vfolder_invitations.c.id == inv_id) &
                          (vfolder_invitations.c.state == 'pending')))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            resp = {'msg': 'No such invitation found.'}
            return web.json_response(resp, status=404)
        query = (vfolder_invitations.update()
                                    .where(vfolder_invitations.c.id == inv_id)
                                    .values(state='rejected'))
        await conn.execute(query)
    resp = {'msg': f'Vfolder invitation is rejected: {inv_id}.'}
    return web.json_response(resp, status=200)


@auth_required
async def delete(request):
    dbpool = request.app['dbpool']
    folder_name = request.match_info['name']
    access_key = request['keypair']['access_key']
    log.info(f"VFOLDER.DELETE (u:{access_key}, f:{folder_name})")
    async with dbpool.acquire() as conn, conn.begin():
        query = (sa.select('*', for_update=True)
                   .select_from(vfolders)
                   .where((vfolders.c.belongs_to == access_key) &
                          (vfolders.c.name == folder_name)))
        try:
            result = await conn.execute(query)
        except psycopg2.DataError as e:
            raise InvalidAPIParameters
        row = await result.first()
        if row is None:
            raise FolderNotFound()
        folder_path = (request.app['VFOLDER_MOUNT'] / row.host / row.id.hex)
        try:
            shutil.rmtree(folder_path)
        except IOError:
            pass
        # TODO: mark it deleted instead of really deleting
        query = (vfolders.delete()
                         .where(vfolders.c.id == row['id']))
        result = await conn.execute(query)
    return web.Response(status=204)


async def init(app):
    mount_prefix = await app['config_server'].etcd.get('volumes/_mount')
    if mount_prefix is None:
        mount_prefix = '/mnt'
    app['VFOLDER_MOUNT'] = Path(mount_prefix)


async def shutdown(app):
    pass


def create_app():
    app = web.Application()
    app['prefix'] = 'folders'
    app['api_versions'] = (2, 3)
    app.on_startup.append(init)
    app.on_shutdown.append(shutdown)
    app.router.add_route('POST',   r'', create)
    app.router.add_route('GET',    r'', list_folders)
    app.router.add_route('GET',    r'/{name}', get_info)
    app.router.add_route('DELETE', r'/{name}', delete)
    app.router.add_route('POST',   r'/{name}/upload', upload)
    app.router.add_route('DELETE', r'/{name}/delete_files', delete_files)
    app.router.add_route('GET',    r'/{name}/download', download)
    app.router.add_route('GET',    r'/{name}/files', list_files)
    app.router.add_route('POST',   r'/{name}/invite', invite)
    app.router.add_route('GET',    r'/invitations/list', invitations)
    app.router.add_route('POST',   r'/invitations/accept', accept_invitation)
    app.router.add_route('DELETE', r'/invitations/delete', delete_invitation)
    return app, []
