"""Personal-file browsing and pagination using mocked Canvas responses."""
import httpx
import pytest

from canvas_py import Canvas, CanvasPermissionError
from canvas_py._config import CanvasConfig
from canvas_py.endpoints.files import GetFolder, ListFolderFiles, ListSubfolders


@pytest.mark.parametrize(("method", "args", "api_path", "payload"), [
    ("list_user_files", (), "users/self/files", [{"id": 1, "filename": "hello.pdf"}]),
    ("list_user_folders", (), "users/self/folders", [{"id": 2, "name": "Root", "parent_folder_id": None}]),
    ("list_folder_files", (2,), "folders/2/files", [{"id": 1, "filename": "hello.pdf"}]),
    ("list_subfolders", (2,), "folders/2/folders", [{"id": 3, "name": "Child", "parent_folder_id": 2}]),
    ("get_folder", (2,), "folders/2", {"id": 2, "name": "Root", "locked_for_user": True}),
    ("user_quota", (), "users/self/files/quota", {"quota": 50, "quota_used": 20}),
])
def test_read_wrapper_paths(method, args, api_path, payload):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with Canvas(CanvasConfig(base_url="https://canvas.test", token="test"), http_client=transport) as canvas:
            result = getattr(canvas.files, method)(*args)
    assert result
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.path == f"/api/v1/{api_path}"


def test_user_file_search_uses_pagination():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=[{"id": 1}], headers={"Link": '<https://canvas.test/api/v1/users/self/files?page=2>; rel="next"'})
        return httpx.Response(200, json=[{"id": 2}])
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with Canvas(CanvasConfig(base_url="https://canvas.test", token="test"), http_client=transport) as canvas:
            assert [file.id for file in canvas.files.list_user_files(search_term="draft", content_types=["application/pdf"])] == [1, 2]
    assert requests[0].url.params["search_term"] == "draft"
    assert requests[0].url.params["content_types[]"] == "application/pdf"


def test_folder_listing_follows_pages_preserving_relationships():
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=[{"id": 1}], headers={"Link": '<https://canvas.test/api/v1/users/self/folders?page=2>; rel="next"'})
        return httpx.Response(200, json=[{"id": 2, "parent_folder_id": 1, "for_submissions": True}])
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with Canvas(CanvasConfig(base_url="https://canvas.test", token="test"), http_client=transport) as canvas:
            folders = canvas.files.list_user_folders()
    assert [folder["id"] for folder in folders] == [1, 2]
    assert folders[1]["parent_folder_id"] == 1 and folders[1]["for_submissions"] is True


@pytest.mark.parametrize("request_type", [GetFolder, ListFolderFiles, ListSubfolders])
@pytest.mark.parametrize("folder_id", [0, -1, True, "2/../files"])
def test_folder_ids_are_positive_integers(request_type, folder_id):
    with pytest.raises(ValueError):
        request_type(folder_id)


def test_permissions_failure_is_not_an_empty_file_list():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"message": "Access denied"}))) as transport:
        with Canvas(CanvasConfig(base_url="https://canvas.test", token="test"), http_client=transport) as canvas:
            with pytest.raises(CanvasPermissionError):
                canvas.files.list_folder_files(2)
