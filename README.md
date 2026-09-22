# Range 回源规划服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：规范化闭区间 / suffix 范围，
按最大间隙合并可共用的回源区间（单次不超过回源上限），返回每个原范围在回源块中
的切片映射，并可凭回源到的块内容按映射无歧义重组原范围字节。

提供两个接口：

- `POST /plan`：规划回源块与切片映射；
- `POST /assemble`：校验并接收按块 Base64 编码的回源内容，按切片映射以原 ranges
  顺序重组，返回每段的 Base64 内容与长度。

## 一键启动（Docker）

需安装 Docker（含 Compose 插件）。在仓库根目录执行：

```sh
# 构建镜像并前台启动（监听 127.0.0.1:8080）
docker compose up --build

# 或后台运行
docker compose up --build -d

# 查看日志 / 停止
docker compose logs -f
docker compose down
```

不使用 Compose 时，也可用 Docker 原生命令一键启动：

```sh
docker build -t range-planner:latest .
docker run --rm -p 127.0.0.1:8080:8080 --name range-planner range-planner:latest
```

健康检查：

```sh
curl http://127.0.0.1:8080/healthz
# {"status":"ok"}
```

## 配置

无需配置；如需更改服务端口，修改 `docker-compose.yml` 中的端口映射
`127.0.0.1:8080:8080`（容器内固定监听 8080）。

## 接口示例

交互式文档：http://127.0.0.1:8080/docs

### 1. 规划回源

```sh
curl -s http://127.0.0.1:8080/plan -H 'Content-Type: application/json' -d '{
  "object_length": 64,
  "ranges": [
    {"type": "interval", "start": 0,  "end": 9},
    {"type": "interval", "start": 5,  "end": 20},
    {"type": "suffix", "last_n": 8}
  ],
  "max_gap": 2,
  "fetch_limit": 32
}'
```

响应中的 `blocks` 为合并后的回源块（含可直接使用的 `range_header`），
`mappings` 为每个原范围在块中的切片映射。

### 2. 回源后组装

按 `blocks[].range_header` 完成实际回源后，把每个块的原始字节做标准 Base64
编码，连同 `/plan` 返回的 `blocks`、`mappings` 原样提交：

```sh
curl -s http://127.0.0.1:8080/assemble -H 'Content-Type: application/json' -d '{
  "blocks":   [ ... ],
  "mappings": [ ... ],
  "contents": [
    {"block_index": 0, "data_base64": "<块 0 字节的 Base64>"},
    {"block_index": 1, "data_base64": "<块 1 字节的 Base64>"}
  ]
}'
```

响应 `segments` 严格按原 `ranges` 顺序（`index` 升序）给出每段的 `start`、
`end`、`length` 与 `data_base64`。

## 错误处理（HTTP 422）

所有语义错误统一返回 422，响应体含可定位的字段路径 `loc`（指向
`blocks` / `mappings` / `contents` 中的具体下标与字段）：

- 字段类型 / 约束错误：`{"error": "invalid_request", "detail": [...]}`；
- `/plan` 范围不可满足：`{"error": "unsatisfiable", ...}`；
- `/assemble` 组装失败：`{"error": "assembly_failed", "message": ..., "loc": [...]}`，
  覆盖缺块、重复块、多余块、Base64 无效、解码后块长度与声明不符、
  切片越界（引用不存在的块、`offset_in_block`/`length` 超出块长）、
  mapping 索引重复、组装长度与范围声明长度不符等情况。

## 本地开发（不使用 Docker）

```sh
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
