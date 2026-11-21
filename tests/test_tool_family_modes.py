from pathlib import Path
import subprocess

from llm_gateway.dashboard import dashboard_mcp_servers, tool_runtime


def test_git_is_one_shared_coding_surface_for_cloud_and_local(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
    assert 'workspace_git' in dashboard_mcp_servers.mode_servers('coding')
    assert 'workspace_git' not in dashboard_mcp_servers.mode_servers('chat')
    cloud = dashboard_mcp_servers.cloud_config(workspace=repo, mode='coding')
    assert 'workspace_git' in cloud['mcp_servers']
    assert 'workspace_git' not in dashboard_mcp_servers.cloud_config(mode='chat')['mcp_servers']
    advertised, dispatch = tool_runtime.available('gpu-rig/example', frozenset({'workspace_git'}), repo)
    names = {row['function']['name'] for row in advertised}
    assert names == {
        'get_workspace_git_status', 'get_workspace_git_log', 'get_workspace_git_diff',
        'get_workspace_git_commit', 'compare_workspace_git_revisions',
        'read_workspace_file_at_revision', 'get_workspace_file_history'}
    result = tool_runtime.call(dispatch, 'get_workspace_git_status', {})
    assert result['structuredContent']['output'] == ''


def test_catalog_retains_family_and_provenance():
    servers = dashboard_mcp_servers.catalog_servers()
    assert servers['workspace_git']['family'] == 'internal'
    assert servers['workspace_git']['source_bundle'] == 'internal'
    assert servers['google_workspace']['family'] == 'google'
    assert servers['google_workspace']['source_bundle'] == 'google'
    assert servers['github_public']['family'] == 'github'
    assert servers['github_public']['source_bundle'] == 'github'


def test_public_github_tools_are_shared_chat_tools():
    assert 'github_public' in dashboard_mcp_servers.mode_servers('chat')
    assert 'github_public' not in dashboard_mcp_servers.mode_servers('coding')
    cloud = dashboard_mcp_servers.cloud_config(mode='chat')
    assert 'github_public' in cloud['mcp_servers']
    advertised, dispatch = tool_runtime.available('gpu-rig/example', frozenset({'github_public'}))
    assert {row['function']['name'] for row in advertised} == {
        'inspect_github_repo', 'list_github_directory', 'find_github_files', 'read_github_file',
        'get_github_commit', 'compare_github_revisions', 'get_github_file_history'}
    assert set(dispatch) == {row['function']['name'] for row in advertised}
