"""FileBlock 渲染与 UserMessage 把文件引用折进 wire content 的行为。"""

from __future__ import annotations

from cyan.llm.types import FileBlock, TextBlock, ToolCallBlock, UserMessage


def test_file_block_renders_path_and_fenced_content():
    block = FileBlock(path="src/a.py", content="print(1)")
    assert block.render() == "[文件 src/a.py]\n```\nprint(1)\n```"


def test_file_block_without_content_renders_header_only():
    block = FileBlock(path="src/a.py")
    assert block.render() == "[文件 src/a.py]"


def test_message_file_blocks_property_filters_out_other_blocks():
    message = UserMessage(
        blocks=[
            TextBlock(text="看看这个"),
            FileBlock(path="a.py", content="x = 1"),
            ToolCallBlock(id="c1", name="read_file"),
        ]
    )
    assert [block.path for block in message.file_blocks] == ["a.py"]


def test_user_message_of_has_no_file_blocks():
    message = UserMessage.of("纯文本任务")
    assert message.file_blocks == []
    assert message.to_api() == {"role": "user", "content": "纯文本任务"}


def test_user_message_to_api_appends_file_blocks_after_text():
    message = UserMessage(
        blocks=[
            TextBlock(text="看看 @a.py 里的逻辑"),
            FileBlock(path="a.py", content="def f(): pass"),
        ]
    )
    payload = message.to_api()
    assert payload["role"] == "user"
    assert payload["content"] == "看看 @a.py 里的逻辑\n\n[文件 a.py]\n```\ndef f(): pass\n```"


def test_user_message_to_api_with_only_file_blocks_and_no_text():
    message = UserMessage(blocks=[FileBlock(path="a.py", content="x = 1")])
    payload = message.to_api()
    assert payload["content"] == "[文件 a.py]\n```\nx = 1\n```"


def test_user_message_to_api_with_multiple_file_blocks():
    message = UserMessage(
        blocks=[
            TextBlock(text="对比一下这两个文件"),
            FileBlock(path="a.py", content="a"),
            FileBlock(path="b.py", content="b"),
        ]
    )
    payload = message.to_api()
    assert payload["content"] == (
        "对比一下这两个文件\n\n[文件 a.py]\n```\na\n```\n\n[文件 b.py]\n```\nb\n```"
    )
