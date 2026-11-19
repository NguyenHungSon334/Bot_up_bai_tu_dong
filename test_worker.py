"""Run: python test_worker.py"""
import worker


def test_parse_results_maps_each_target_and_fills_missing():
    text = ('blah\nRESULT: [{"id": 1, "status": "failed"}]\n'
            'RESULT: [{"id": 1, "status": "done", "post_url": "https://facebook.com/x"},'
            ' {"id": 2, "status": "pending_approval"}, {"id": 99, "status": "done"}]')
    r = worker.parse_results(text, [1, 2, 3])
    assert r[1] == {"status": "done", "post_url": "https://facebook.com/x", "error": None}
    assert r[2]["status"] == "pending_approval"
    assert r[3]["status"] == "failed" and "không báo" in r[3]["error"]
    assert 99 not in r


def test_parse_results_rejects_garbage():
    assert all(v["status"] == "failed" for v in worker.parse_results("", [1, 2]).values())
    assert worker.parse_results("RESULT: [bad json]", [1])[1]["status"] == "failed"
    assert worker.parse_results('RESULT: [{"id": 1, "status": "posted"}]', [1])[1]["status"] == "failed"


def test_overall_status():
    assert worker.overall_status(["done", "pending_approval"]) == "done"
    assert worker.overall_status(["done", "failed"]) == "partial"
    assert worker.overall_status(["failed"]) == "failed"


def test_build_prompt_lists_all_targets_and_keeps_content_literal():
    post = {"id": 7, "images": '["C:/m/a.png"]', "content": "Xin chào {{TARGETS}}", "only_me": 0}
    deliveries = [{"id": 11, "kind": "page", "name": "Metory", "url": "https://www.facebook.com/1/"},
                  {"id": 12, "kind": "group", "name": "G", "url": "https://www.facebook.com/groups/2"}]
    p = worker.build_prompt(post, deliveries)
    assert "Xin chào {{TARGETS}}" in p
    assert "1. id=11 | Page | Metory" in p and "2. id=12 | Group | G" in p
    assert "- C:/m/a.png" in p and "post_7_" in p
    assert "mặc định (không đổi)" in p and "{{PRIVACY}}" not in p


def test_build_prompt_private_profile():
    post = {"id": 8, "images": "[]", "content": "x", "only_me": 1}
    p = worker.build_prompt(post, [{"id": 1, "kind": "profile", "name": "Tôi", "url": "https://www.facebook.com/me"}])
    assert "| Trang cá nhân |" in p and "CHỈ MÌNH TÔI — bắt buộc" in p


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK")
