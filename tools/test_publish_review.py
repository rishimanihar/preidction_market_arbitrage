import unittest
from publish_review import TRACKER, GitHub, publish, render_report


class FakeAPI:
    def __init__(self):
        self.issues, self.comments, self.posts = [], [], []

    def pages(self, path):
        return iter(self.comments if "/comments" in path else self.issues)

    def request(self, method, path, body):
        self.posts.append((path, body))
        row = {"number": 17, "body": body["body"], "html_url": "https://github.com/example/result"}
        (self.comments if "/comments" in path else self.issues).append(row)
        return row


class Tests(unittest.TestCase):
    def test_create_issue_and_comment(self):
        api = FakeAPI()
        self.assertEqual(publish(api, "# Synthetic report")["status"], "published")
        self.assertIn(TRACKER, api.issues[0]["body"])
        self.assertIn("@avyukthNarra", api.comments[0]["body"])

    def test_deduplicate_across_line_endings(self):
        api = FakeAPI()
        publish(api, "# Report\r\nOne finding\r\n")
        self.assertEqual(publish(api, "# Report\nOne finding\n")["status"], "already-published")
        self.assertEqual(len(api.posts), 2)

    def test_revised_report_reuses_issue(self):
        api = FakeAPI()
        publish(api, "Original")
        publish(api, "Revised")
        self.assertEqual((len(api.issues), len(api.comments)), (1, 2))

    def test_pr_not_mistaken_for_tracker(self):
        api = FakeAPI()
        api.issues.append({"pull_request": {}, "body": TRACKER})
        publish(api, "Report")
        self.assertEqual(len(api.issues), 2)

    def test_ambiguous_trackers_stop_writes(self):
        api = FakeAPI()
        api.issues = [{"body": TRACKER}, {"body": TRACKER}]
        with self.assertRaises(RuntimeError):
            publish(api, "Report")
        self.assertFalse(api.posts)

    def test_invalid_report(self):
        for value in (" ", "x" * 58001):
            with self.assertRaises(ValueError):
                render_report(value)

    def test_credentials_cannot_be_redirected(self):
        with self.assertRaises(ValueError):
            GitHub("https://example.com/steal", "synthetic")
