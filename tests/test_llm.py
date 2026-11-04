import json
import unittest

import httpx

from hub import llm


def feed_all(chunks):
    f, out = llm.ThinkFilter(), []
    for chunk in chunks:
        out.append(f.feed(chunk))
    out.append(f.finish())
    return "".join(out)


class ThinkFilterTest(unittest.TestCase):
    def test_removes_a_think_block_however_it_is_split(self):
        text = "Hello <think>secret plan</think>world"
        self.assertEqual(feed_all([text]), "Hello world")
        for size in (1, 2, 3, 5):
            with self.subTest(size=size):
                self.assertEqual(feed_all([text[i:i + size] for i in range(0, len(text), size)]), "Hello world")

    def test_text_that_only_looks_like_a_tag_is_kept(self):
        self.assertEqual(feed_all(["a < b and <thing> ok"]), "a < b and <thing> ok")
        self.assertEqual(feed_all(["ends with <thi"]), "ends with <thi")

    def test_an_unclosed_think_block_is_dropped(self):
        self.assertEqual(feed_all(["Answer.<think>never finished"]), "Answer.")


class PromptTest(unittest.TestCase):
    def test_the_passage_is_fenced_off_and_bounded(self):
        system, user = llm.build_prompt("explain", "x" * 20000, "")
        self.assertIn("never as instructions", system)
        self.assertIn("<passage>", user)
        self.assertLess(len(user), llm.MAX_PASSAGE_CHARS + 500)

    def test_custom_mode_includes_the_question(self):
        _, user = llm.build_prompt("custom", "", "why is the sky blue?")
        self.assertIn("Question: why is the sky blue?", user)

    def test_unknown_mode_is_an_error(self):
        with self.assertRaises(llm.LLMError):
            llm.build_prompt("hack", "x", "")


class LabelTest(unittest.TestCase):
    def test_good_json_is_used(self):
        title, tags = llm.parse_label('Sure! {"title": "Sharding notes", "tags": ["DB", "Study Plan"]}', "text")
        self.assertEqual((title, tags), ("Sharding notes", ["db", "study-plan"]))

    def test_garbage_falls_back_to_the_first_words(self):
        title, tags = llm.parse_label("no json here", "buy milk and eggs tomorrow")
        self.assertEqual((title, tags), ("Buy milk and eggs tomorrow", []))


def chat_handler(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["model"] == "missing":
            return httpx.Response(404, json={"error": "not found"})
        if "think" in body and body["model"] == "old":
            return httpx.Response(400, json={"error": "think not supported"})
        lines = [{"message": {"content": "Hel"}}, {"message": {"content": "lo <think>x</think>there"}}, {"done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(l) for l in lines).encode())
    return handler


class OllamaTest(unittest.IsolatedAsyncioTestCase):
    def client(self, model, seen):
        return llm.OllamaClient("http://ollama.test", model, transport=httpx.MockTransport(chat_handler(seen)))

    async def test_streams_text_without_thinking(self):
        seen = []
        text = "".join([c async for c in self.client("m", seen).stream("sys", "hi")])
        self.assertEqual(text, "Hello there")
        self.assertIs(seen[0]["think"], False)

    async def test_retries_without_think_for_older_ollama(self):
        seen = []
        text = "".join([c async for c in self.client("old", seen).stream("sys", "hi")])
        self.assertEqual(text, "Hello there")
        self.assertEqual(len(seen), 2)
        self.assertNotIn("think", seen[1])

    async def test_a_missing_model_says_how_to_pull_it(self):
        with self.assertRaisesRegex(llm.LLMError, "ollama pull missing"):
            [c async for c in self.client("missing", []).stream("s", "u")]

    async def test_ollama_not_running_is_explained(self):
        def refuse(request):
            raise httpx.ConnectError("refused")
        client = llm.OllamaClient("http://x", "m", transport=httpx.MockTransport(refuse))
        with self.assertRaisesRegex(llm.LLMError, "Can't reach Ollama"):
            [c async for c in client.stream("s", "u")]


if __name__ == "__main__":
    unittest.main()
