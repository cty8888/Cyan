"""vulture 的白名单：已确认是误报或有意保留的"未使用"符号，不代表真的没用。

- ``on_text_changed``：通过 ``+=`` 注册事件处理器（见 ``cli/app.py``），vulture 的静态分析
  认不出这种写法。
- ``complete_event``：``prompt_toolkit.Completer.get_completions`` 接口要求的参数签名，
  不用也必须保留（见 ``cli/completion.py``）。
- ``start_line`` / ``end_line``：``FileBlock`` 的字段，``@path`` 引用目前只按整篇文件构造
  （见 ``cli/file_refs.py``），会话事件里也会带着一起序列化/反序列化，但赋的值始终是
  ``None``——留给以后 "@file:10-20" 这种带行号范围的引用用（见 ``llm/types.py``）。
- ``language``：``CodeBlock`` 的字段，Block 内容模型里预留的扩展类型，目前还没有代码路径
  真正构造它（见 ``llm/types.py``）。
- ``finished_at``：``ToolExecution`` 记录工具结束时间，目前只写入不读取
  （``duration`` 才是被实际使用的派生值），保留作为可能的未来调试/展示字段。
- ``_home``：测试辅助类里存了但没读的属性（见 ``tests/test_commands.py``），纯测试代码。

跑法：``uv run vulture src/ tests/ vulture_whitelist.py --min-confidence 60``。
新出现的误报按同样格式追加；新出现的真死代码请直接删掉源码，不要塞进这份白名单。
"""

_.on_text_changed  # unused attribute (src/cyan/cli/app.py:114)
complete_event  # unused variable (src/cyan/cli/completion.py:29)
start_line  # unused variable (src/cyan/llm/types.py:79)
end_line  # unused variable (src/cyan/llm/types.py:80)
language  # unused variable (src/cyan/llm/types.py:88)
_.finished_at  # unused attribute (src/cyan/session/session.py:239)
finished_at  # unused variable (src/cyan/session/types.py:164)
_._home  # unused attribute (tests/test_commands.py:298)
