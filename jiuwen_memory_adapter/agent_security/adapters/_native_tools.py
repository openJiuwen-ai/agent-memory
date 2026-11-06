"""普通原生工具构造；只由相应运行时驱动使用，不承担安全门禁。"""


def create_openjiuwen_tool(*, name, description, schema, ainvoke):
    from openjiuwen.core.foundation.tool import Tool, ToolCard

    class NativeTool(Tool):
        def __init__(self):
            super().__init__(ToolCard(name=name, description=description, input_params=schema))

        async def invoke(self, inputs, session=None, **kwargs):
            return await ainvoke(**inputs)

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs, **kwargs)

    return NativeTool()
