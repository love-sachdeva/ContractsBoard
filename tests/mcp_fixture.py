"""Local protocol fixture; never calls Notion or Google."""
import base64
import json
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP

SOURCE = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
PAGE = '11111111-1111-4111-8111-111111111111'
FOLDER = 'fixture_contracts_folder'
UPLOAD = 'fixture_uploads_folder'
state = {'pages': {PAGE: {'Partner': 'Fixture partner', 'Queue': 'To pick up', 'Contract stage': 'Negotiating', 'Notes': 'Fixture only', 'Contract link': '', 'Owner': '', 'Needs verification': False}}, 'files': {}, 'calls': [], 'has_more': False, 'fail_update': False, 'second_page': True}
mcp = FastMCP('Portal integration fixture', stateless_http=True, json_response=True)


@mcp.tool(name='notion-fetch')
def fetch(id: str) -> dict:
    state['calls'].append(['fetch', id])
    return {'id': id, 'properties': state['pages'][id]}


@mcp.tool(name='notion-query-data-sources')
def query(mode: str, data_source_url: str, limit: int) -> dict:
    assert data_source_url == 'collection://' + SOURCE
    state['calls'].append(['query'])
    return {'results': [{'url': 'https://www.notion.so/' + key.replace('-', ''), **props} for key, props in state['pages'].items()], 'has_more': state['has_more']}


@mcp.tool(name='notion-update-page')
def update(page_id: str, command: str, properties: dict) -> dict:
    assert command == 'update_properties'
    state['calls'].append(['update', properties])
    if state['fail_update']:
        raise ValueError('Permission fixture failure')
    state['pages'][page_id].update(properties)
    return {'page_id': page_id}


@mcp.tool(name='notion-create-pages')
def create_pages(parent: dict, pages: list[dict], allow_async: bool) -> dict:
    assert parent['data_source_id'] == SOURCE
    state['calls'].append(['create_partner'])
    created = []
    for page in pages:
        key = str(uuid.uuid4())
        state['pages'][key] = page['properties']
        created.append({'id': key, 'url': 'https://www.notion.so/' + key.replace('-', '')})
    return {'pages': created}


@mcp.tool(name='get_file_metadata')
def get_metadata(fileId: str) -> dict:
    state['calls'].append(['metadata', fileId])
    if fileId in (FOLDER, UPLOAD):
        return {'id': fileId, 'title': 'Fixture folder', 'mimeType': 'application/vnd.google-apps.folder', 'canAddChildren': True}
    return {k: v for k, v in state['files'][fileId].items() if k != 'content'}


@mcp.tool(name='search_files')
def search_files(query: str, pageSize: int, excludeContentSnippets: bool, pageToken: str = '') -> dict:
    state['calls'].append(['search', query, pageToken])
    folder = query.split("'")[1]
    files = [{k: v for k, v in file.items() if k != 'content'} for file in state['files'].values() if file['parentId'] == folder]
    if folder == FOLDER and not pageToken and state['second_page']:
        return {'files': files[:1], 'nextPageToken': 'fixture-page-2'}
    return {'files': files[1:] if pageToken else files}


@mcp.tool(name='create_file')
def create_file(title: str, contentMimeType: str, base64Content: str, parentId: str, disableConversionToGoogleType: bool) -> dict:
    assert parentId == UPLOAD and disableConversionToGoogleType
    base64.b64decode(base64Content, validate=True)
    state['calls'].append(['upload', title])
    key = 'fixturefile_' + uuid.uuid4().hex
    item = {'id': key, 'title': title, 'mimeType': contentMimeType, 'parentId': parentId, 'content': base64Content, 'viewUrl': 'https://drive.google.com/file/d/' + key + '/view'}
    state['files'][key] = item
    return {k: v for k, v in item.items() if k != 'content'}


@mcp.tool(name='download_file_content')
def download_file_content(fileId: str, exportMimeType: str = '') -> dict:
    return state['files'][fileId]


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)
app.mount('/provider', mcp.streamable_http_app())


@app.get('/fixture/state')
def inspect_state():
    return state


@app.post('/fixture/reset')
def reset():
    state['pages'] = {PAGE: {'Partner': 'Fixture partner', 'Queue': 'To pick up', 'Contract stage': 'Negotiating', 'Notes': 'Fixture only', 'Contract link': '', 'Owner': '', 'Needs verification': False}}
    state.update(files={}, calls=[], has_more=False, fail_update=False, second_page=True)
    return {'ok': True}


@app.post('/fixture/config')
def configure(payload: dict):
    state.update(payload)
    return {'ok': True}
