# Default Lambda MicroVM sandbox image: the CAS SDK's app server (app.py),
# installed from this checkout. Old CAS drives it through the harness API.
#
# Build from the repository root (MicroVMs are arm64), after creating the
# app's settings file (the engine):
#   cp examples/sdk_default_lambda_microvm/.env.example examples/sdk_default_lambda_microvm/.env
#   docker buildx build --platform linux/arm64 \
#     -f examples/sdk_default_lambda_microvm/Dockerfile -t sdk-default-lambda-microvm .
#
# Nothing per session is baked in: old CAS passes the agent id, the sandbox
# token and the egress routes in POST /v1/sessions envs.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The SDK, from this checkout. With a published release, replace these three
# lines with: RUN pip install 'computer-agent-py[agentos,server,gitagent,otel]'
WORKDIR /opt/computer-agent-py
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[agentos,server,gitagent,otel]" "python-dotenv>=1,<2"

# The app and its settings; app.py loads .env, and docker run -e overrides it.
COPY examples/sdk_default_lambda_microvm/app.py examples/sdk_default_lambda_microvm/.env /app/

# The Claude CLI refuses to skip permission prompts as root; the engine runs
# in the working directory.
RUN useradd --create-home --uid 10001 agent && mkdir /workspace && chown agent:agent /workspace
USER agent
WORKDIR /workspace

EXPOSE 8080
CMD ["python", "/app/app.py"]
