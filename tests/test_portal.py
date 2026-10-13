import base64
import hashlib
import io
import json
import time
import uuid

import pytest
import requests
from docx import Document

from mcp_connections import ConnectionError, WriteUncertainError, fit_arguments, unpack_result
from mcp_services import properties


def sync(client):
    response = client.post('/api/sync', json={})
    assert response.status_code == 200
    result = response.json()
    assert result['notion']['success'], result
    assert result['drive']['success'], result
    return client.get('/api/overview').json()['partners'][0]


def docx():
    document = Document()
    document.add_paragraph('Commission shall be 30 percent. Termination requires 30 days notice.')
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def test_protocol_discovery(portal):
    module, client, provider = portal
    assert any(t['name']=='notion-fetch' for t in module.connections.list_tools('notion'))


def test_protocol_import_move_stage_and_source_status(portal):
    module, client, provider = portal
    partner = sync(client)
    response = client.post('/api/partners/' + partner['id'] + '/move', json={'queue': 'Saket review'})
    assert response.json()['sync_state'] == 'live'
    response = client.post('/api/partners/' + partner['id'] + '/stage', json={'stage': 'For signature'})
    assert response.json()['sync_state'] == 'live'
    actual = requests.get(provider + '/fixture/state').json()['pages'][partner['notion_id']]
    assert actual['Queue'] == 'Saket review' and actual['Contract stage'] == 'For signature'
    overview = client.get('/api/overview')
    assert overview.json()['mode'] == 'live'
    assert 'fixture-secret-token' not in overview.text
    assert 'fixture-refresh-token' not in client.get('/api/settings').text


def test_upload_link_duplicate_and_retry_without_second_upload(portal):
    module, client, provider = portal
    partner = sync(client)
    data = {'partner_id': partner['id'], 'document_type': 'Reseller agreement', 'note': 'Partner redlines'}
    raw = docx()
    response = client.post('/api/upload', data=data, files={'file': ('Fixture partner.docx', raw)})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'synced', result
    assert result['file']['note'] == 'Partner redlines'
    assert client.post('/api/upload', data=data, files={'file': ('Fixture partner.docx', raw)}).json()['duplicate']
    assert client.post('/api/files/' + result['file']['id'] + '/retry').json()['success']
    state = requests.get(provider + '/fixture/state').json()
    assert len([c for c in state['calls'] if c[0] == 'upload']) == 1
    assert state['pages'][partner['notion_id']]['Contract link'] == result['file']['drive_url']


def test_notion_failure_retains_file_and_retry_only_links(portal):
    module, client, provider = portal
    partner = sync(client)
    requests.post(provider + '/fixture/config', json={'fail_update': True})
    response = client.post('/api/upload', data={'partner_id': partner['id']}, files={'file': ('contract.docx', docx())})
    result = response.json()
    assert result['status'] == 'notion_pending'
    requests.post(provider + '/fixture/config', json={'fail_update': False})
    assert client.post('/api/files/' + result['file']['id'] + '/retry').json()['success']
    state = requests.get(provider + '/fixture/state').json()
    assert len([c for c in state['calls'] if c[0] == 'upload']) == 1


def test_pending_move_survives_read_only_refresh(portal):
    module, client, provider = portal
    partner = sync(client)
    requests.post(provider + '/fixture/config', json={'fail_update': True})
    assert client.post('/api/partners/' + partner['id'] + '/move', json={'queue': 'On hold'}).json()['sync_state'] == 'pending'
    before = requests.get(provider + '/fixture/state').json()['calls']
    client.post('/api/sync')
    after = requests.get(provider + '/fixture/state').json()['calls']
    assert sum(c[0] == 'update' for c in before) == sum(c[0] == 'update' for c in after)
    assert client.get('/api/overview').json()['partners'][0]['queue'] == 'On hold'
    requests.post(provider + '/fixture/config', json={'fail_update': False})
    assert client.post('/api/partners/publish-pending').json()['results'][0]['success']


def test_demo_records_never_published(portal):
    module, client, provider = portal
    key = str(uuid.uuid4())
    module.execute("INSERT INTO partners(id,name,queue,stage,source,sync_state) VALUES(?,?,?,?,?,?)", (key, 'Demo partner', 'To pick up', 'Not received', 'demo', 'pending'))
    assert client.post('/api/partners/publish-pending').json()['results'] == []
    assert not any(c[0] == 'create_partner' for c in requests.get(provider + '/fixture/state').json()['calls'])
    assert client.post('/api/upload', data={'partner_id': key}, files={'file': ('x.docx', docx())}).status_code == 400


def test_create_live_partner_verifies_before_reporting_success(portal):
    module, client, provider = portal
    sync(client)
    response = client.post('/api/partners', json={'name': 'New partner'})
    assert response.json()['partner']['sync_state'] == 'live', response.text
    assert client.post('/api/partners', json={'name': 'New partner'}).json()['existing']


def test_partial_database_is_not_imported(portal):
    module, client, provider = portal
    requests.post(provider + '/fixture/config', json={'has_more': True})
    result = client.post('/api/sync').json()
    assert not result['notion']['success']
    assert module.rows('SELECT * FROM partners') == []
    assert client.get('/api/overview').json()['mode'] != 'live'


def test_drive_pagination_is_followed(portal):
    module, client, provider = portal
    sync(client)
    calls = requests.get(provider + '/fixture/state').json()['calls']
    assert any(c[0] == 'search' and c[2] == 'fixture-page-2' for c in calls)


def test_wrong_type_upload_is_rejected(portal):
    module, client, provider = portal
    partner = sync(client)
    assert client.post('/api/upload', data={'partner_id': partner['id']}, files={'file': ('false.pdf', b'PKfake')}).status_code == 400
    assert client.post('/api/upload', data={'partner_id': partner['id']}, files={'file': ('false.docx', b'%PDF')}).status_code == 400


def test_authentication_and_exact_origin_check(portal):
    module, client, provider = portal
    assert client.get('/api/overview', auth=('finance', 'bad')).status_code == 401
    assert client.post('/api/sync', headers={'Origin': 'http://evillocalhost:3000'}).status_code == 403
    assert client.post('/api/sync', headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.post('/api/sync', headers={'Origin': 'http://localhost:3000'}).status_code == 200


def test_source_settings_accept_existing_folder_and_reset_health(portal):
    module, client, provider = portal
    sync(client)
    response = client.post('/api/settings', json={'upload_folder_id': 'https://drive.google.com/drive/folders/existingfolder123'})
    assert response.json()['saved']
    assert client.get('/api/settings').json()['upload_folder_id'] == 'existingfolder123'
    assert not client.get('/api/settings').json()['google_verified']
    assert client.post('/api/settings', json={'GOOGLE_CLIENT_SECRET': 'bad'}).status_code == 400


def test_uncertain_upload_is_not_replayed_and_can_be_reconciled(portal):
    module, client, provider = portal
    partner = sync(client)
    raw = docx()
    result = client.post('/api/upload', data={'partner_id': partner['id']}, files={'file': ('recover.docx', raw)}).json()
    fid, driveid = result['file']['id'], result['file']['drive_id']
    module.execute("UPDATE files SET sync_state='drive_uncertain',drive_id='',drive_url='' WHERE id=?", (fid,))
    assert not client.post('/api/files/' + fid + '/retry').json()['success']
    assert client.post('/api/files/' + fid + '/resolve', json={'drive_id': driveid}).json()['success']
    assert sum(c[0] == 'upload' for c in requests.get(provider + '/fixture/state').json()['calls']) == 1


def test_encrypted_store_and_disconnect(portal):
    module, client, provider = portal
    row = module.one("SELECT value FROM settings WHERE key='mcp:notion:grant'")
    assert 'fixture-secret-token' not in row['value']
    assert client.post('/api/connections/notion/disconnect').json()['disconnected']
    assert not module.connections.connected('notion')


def test_oauth_state_cookie_binding_expiry_and_single_use(portal, monkeypatch):
    module, client, provider = portal
    connections = module.connections
    monkeypatch.setattr(connections, 'metadata', lambda p: {'authorization_endpoint': 'https://mcp.notion.com/authorize', 'token_endpoint': 'https://mcp.notion.com/token'})
    monkeypatch.setattr(connections, 'credentials', lambda p, m: {'client_id': 'fixture-client'})
    captured = []
    def exchange(endpoint, params):
        captured.append(params)
        return {'access_token': 'new-secret', 'refresh_token': 'new-refresh', 'expires_in': 3600}
    monkeypatch.setattr(connections, 'exchange', exchange)
    from urllib.parse import parse_qs, urlparse
    url, cookie = connections.begin('notion')
    params = parse_qs(urlparse(url).query)
    state = params['state'][0]
    with pytest.raises(ConnectionError):
        connections.finish('notion', state, 'other-browser', 'fixture-code')
    assert captured == []
    connections.finish('notion', state, cookie, 'fixture-code')
    assert captured[0]['code_verifier'] != params['code_challenge'][0]
    assert connections.connected('notion')
    with pytest.raises(ConnectionError):
        connections.finish('notion', state, cookie, 'fixture-code')
    url, cookie = connections.begin('notion')
    state = parse_qs(urlparse(url).query)['state'][0]
    with connections.database() as db:
        db.execute('UPDATE oauth_flows SET expires=0')
    with pytest.raises(ConnectionError):
        connections.finish('notion', state, cookie, 'fixture-code')


def test_rotated_refresh_token_persisted_and_terminal_expiry(portal, monkeypatch):
    module, client, provider = portal
    connections = module.connections
    grant = {'access_token': 'old', 'refresh_token': 'old-refresh', 'expires_at': 0, 'client': {'client_id': 'fixture-client'}, 'metadata': {'token_endpoint': 'https://mcp.notion.com/token'}}
    connections.write('notion:grant', grant)
    monkeypatch.setattr(connections, 'exchange', lambda e, p: {'access_token': 'rotated', 'refresh_token': 'rotated-refresh', 'expires_in': 3600})
    with connections.locks['notion']:
        assert connections.access_token('notion') == 'rotated'
    assert connections.read('notion:grant')['refresh_token'] == 'rotated-refresh'
    grant = connections.read('notion:grant'); grant['expires_at'] = 0; connections.write('notion:grant', grant)
    def rejected(e, p):
        raise ConnectionError('reauth_required')
    monkeypatch.setattr(connections, 'exchange', rejected)
    with pytest.raises(ConnectionError):
        with connections.locks['notion']:
            connections.access_token('notion')
    assert not connections.connected('notion')


def test_error_payloads_schema_validation_and_xml_properties():
    with pytest.raises(ConnectionError):
        unpack_result({'isError': True, 'content': [{'type': 'text', 'text': 'secret-token'}]})
    with pytest.raises(ConnectionError):
        fit_arguments({'type': 'object', 'properties': {'page_id': {'type': 'string'}}, 'required': ['page_id']}, {'wrong': 'id'})
    assert fit_arguments({'type': 'object', 'properties': {'data': {'type': 'object'}}, 'required': ['data']}, {'page_id': 'id'}) == {'data': {'page_id': 'id'}}
    assert properties({'text': '<page><properties>{"Queue":"On hold"}</properties></page>'})['Queue'] == 'On hold'


def test_sdk_schema_rejection_does_not_send_write(portal):
    module, client, provider = portal
    with pytest.raises(ConnectionError):
        module.connections.call('google', 'create_file', {'title': 'missing required fields'})
    assert not any(c[0] == 'upload' for c in requests.get(provider + '/fixture/state').json()['calls'])


def test_source_switch_cannot_write_old_partners(portal):
    module, client, provider = portal
    partner=sync(client)
    response=client.post('/api/settings',json={'notion_data_source_id':'22222222-2222-4222-8222-222222222222'})
    assert response.json()['saved']
    assert client.get('/api/overview').json()['partners']==[]
    assert client.post('/api/partners/'+partner['id']+'/move',json={'queue':'On hold'}).status_code==409


def test_uncertain_partner_create_can_be_reconciled_without_duplicate(portal,monkeypatch):
    module,client,provider=portal
    sync(client)
    original=module.connections.call
    def timeout_after_create(provider_name,name,args):
        result=original(provider_name,name,args)
        if name=='notion-create-pages':raise WriteUncertainError('Fixture lost write response')
        return result
    monkeypatch.setattr(module.connections,'call',timeout_after_create)
    result=client.post('/api/partners',json={'name':'Uncertain partner'}).json()
    assert result['partner']['sync_state']=='publishing'
    assert client.post('/api/partners/publish-pending').json()['results']==[]
    state=requests.get(provider+'/fixture/state').json()
    page_id=next(k for k,v in state['pages'].items() if v['Partner']=='Uncertain partner')
    assert client.post('/api/partners/'+result['partner']['id']+'/resolve',json={'notion_id':page_id}).json()['saved']
    assert sum(c[0]=='create_partner' for c in requests.get(provider+'/fixture/state').json()['calls'])==1


def test_dynamic_registration_credentials_are_reused(portal,monkeypatch):
    module,client,provider=portal
    calls=[]
    class Response:
        status_code=201
        def json(self):return {'client_id':'registered-client'}
    def register(url,**kwargs):
        calls.append(kwargs['json'])
        return Response()
    monkeypatch.setattr(requests,'post',register)
    metadata={'registration_endpoint':'https://mcp.notion.com/register'}
    first=module.connections.credentials('notion',metadata)
    assert module.connections.credentials('notion',metadata)==first
    assert len(calls)==1
    assert calls[0]['token_endpoint_auth_method']=='none'
    assert calls[0]['redirect_uris']==['http://localhost:3000/auth/notion/callback']


def test_oauth_metadata_rejects_untrusted_endpoint():
    from mcp_connections import Connections
    with pytest.raises(ConnectionError):
        Connections.checked_notion_url('https://evil.test/token')
