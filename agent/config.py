import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    llm_model: str = os.getenv("AGENT_LLM_MODEL", "qwen3.8-flash")
    llm_model_compress: str = os.getenv("AGENT_LLM_MODEL_COMPRESS", "qwen-turbo")
    dashscope_base_url: str = os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    context_limit: int = int(os.getenv("AGENT_CONTEXT_LIMIT", "128000"))
    compress_threshold: float = float(os.getenv("AGENT_COMPRESS_THRESHOLD", "0.7"))
    tool_output_limit: int = int(os.getenv("AGENT_TOOL_OUTPUT_LIMIT", "8000"))
    sandbox_url: str = os.getenv("AGENT_SANDBOX_URL", "http://localhost:8080")
    sandbox_enabled: str = os.getenv("AGENT_SANDBOX_ENABLED", "auto")
    repo_root: str = os.getenv("AGENT_REPO_ROOT", os.getcwd())
    target_branch: str = os.getenv("AGENT_TARGET_BRANCH", "")
    auto_merge: bool = os.getenv("AGENT_AUTO_MERGE", "true").lower() == "true"

    # 预算控制
    max_iterations: int = int(os.getenv("AGENT_MAX_ITERATIONS", "30"))
    max_tokens: int = int(os.getenv("AGENT_MAX_TOKENS", "200000"))
    max_duration_seconds: int = int(os.getenv("AGENT_MAX_DURATION_SECONDS", "600"))


settings = Settings()