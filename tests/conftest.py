"""为确定性测试固定本地配置，禁止测试收集阶段调用在线模型。"""

import os


# pytest 会先加载本文件，再导入 app/store；显式覆盖 .env 中的在线 Embedding 设置。
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ.setdefault("OPENAI_API_KEY", "test")
