# Range 回源规划服务

为对象存储代理规划 HTTP Range 回源的 FastAPI 服务：规范化闭区间 / suffix 范围，
按最大间隙合并可共用的回源区间（单次不超过回源上限），并返回每个原范围在回源块中
的切片映射。

## 启动

```
docker compose up --build
```

## 配置

无需配置；如需更改服务端口，修改 `docker-compose.yml` 中的端口映射
`127.0.0.1:8080:8080`（容器内固定监听 8080）。

## 访问

http://localhost:8080 （接口文档：http://localhost:8080/docs ，规划接口 `POST /plan`）
