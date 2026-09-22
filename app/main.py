"""HTTP Range 回源规划服务。

POST /plan 接收对象总长度、若干闭区间或 suffix 范围、允许合并的最大间隙
以及单次回源上限，输出合并后的回源块，以及每个请求范围在回源块中的
切片映射，使重叠范围可以复用字节并按映射无歧义重组。
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt

app = FastAPI(
    title="Range 回源规划服务",
    description="为对象存储代理规划可合并、带上限的 HTTP Range 回源。",
)


# ----------------------------- 请求模型 -----------------------------


class IntervalRange(BaseModel):
    """闭区间范围 [start, end]，两端均为包含端点的字节偏移。"""

    type: Literal["interval"]
    start: Annotated[int, StrictInt] = Field(..., ge=0, description="起始字节偏移（含）")
    end: Annotated[int, StrictInt] = Field(..., ge=0, description="结束字节偏移（含）")


class SuffixRange(BaseModel):
    """suffix 范围：对象末尾的 last_n 个字节。"""

    type: Literal["suffix"]
    last_n: Annotated[int, StrictInt] = Field(..., ge=1, description="请求末尾的字节数")


RangeSpec = Annotated[
    Union[IntervalRange, SuffixRange],
    Field(discriminator="type"),
]


class PlanRequest(BaseModel):
    object_length: Annotated[int, StrictInt] = Field(..., ge=0, description="对象总字节长度")
    ranges: list[RangeSpec] = Field(..., min_length=1, description="请求范围列表，可为闭区间或 suffix")
    max_gap: Annotated[int, StrictInt] = Field(..., ge=0, description="允许合并的最大间隙（字节）")
    fetch_limit: Annotated[int, StrictInt] = Field(..., ge=1, description="单次回源上限（字节）")


# ----------------------------- 响应模型 -----------------------------


class SliceOut(BaseModel):
    block_index: int = Field(..., description="回源块编号")
    block_start: int = Field(..., description="回源块起始字节偏移（含）")
    block_end: int = Field(..., description="回源块结束字节偏移（含）")
    offset_in_block: int = Field(..., description="请求范围在回源块中的起始偏移")
    length: int = Field(..., ge=1, description="该切片字节数")


class RangeMapping(BaseModel):
    index: int = Field(..., description="该范围在原请求 ranges 中的下标")
    type: Literal["interval", "suffix"]
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    slices: list[SliceOut] = Field(..., description="在各回源块中的切片映射（长度恰为 1）")


class FetchBlock(BaseModel):
    index: int
    start: int = Field(..., description="回源起始字节偏移（含）")
    end: int = Field(..., description="回源结束字节偏移（含）")
    length: int = Field(..., description="回源字节数，不超过 fetch_limit")
    range_header: str = Field(..., description="可直接用于回源的 HTTP Range 头值")


class PlanResponse(BaseModel):
    object_length: int
    blocks: list[FetchBlock]
    mappings: list[RangeMapping]


# ----------------------------- 错误处理 -----------------------------


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """以统一的 422 结构报告字段校验失败（越界、类型错误等）。"""
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_request", "detail": exc.errors()},
    )


class PlanningError(Exception):
    """语义校验失败（范围不可满足等），对应 HTTP 422。"""

    def __init__(self, message: str, range_index: int | None = None):
        self.message = message
        self.range_index = range_index
        super().__init__(message)


@app.exception_handler(PlanningError)
async def planning_error_handler(request: Request, exc: PlanningError):
    return JSONResponse(
        status_code=422,
        content={
            "error": "unsatisfiable",
            "message": exc.message,
            **({"range_index": exc.range_index} if exc.range_index is not None else {}),
        },
    )


# ----------------------------- 核心规划逻辑 -----------------------------


def normalize(req: PlanRequest) -> list[tuple[int, int]]:
    """把每个范围规范化为闭区间 [start, end]，并拒绝越界或不可满足输入。"""
    normalized: list[tuple[int, int]] = []
    for i, spec in enumerate(req.ranges):
        if spec.type == "interval":
            start, end = spec.start, spec.end
            if end < start:
                raise PlanningError("end 不得小于 start", i)
            if end >= req.object_length:
                raise PlanningError(
                    f"区间 [{start},{end}] 越界：对象长度为 {req.object_length}", i
                )
        else:  # suffix
            # suffix 在对象不足 last_n 字节时按 HTTP 语义回落到整个对象
            start = max(0, req.object_length - spec.last_n)
            end = req.object_length - 1
            if start > end:
                raise PlanningError("空对象无法满足 suffix 范围请求", i)

        if end - start + 1 > req.fetch_limit:
            raise PlanningError(
                f"范围 [{start},{end}] 长度 {end - start + 1} 超过单次回源上限 "
                f"{req.fetch_limit}，无法规划",
                i,
            )
        normalized.append((start, end))
    return normalized


def build_plan(
    normalized: list[tuple[int, int]], max_gap: int, fetch_limit: int
) -> tuple[list[tuple[int, int]], list[int]]:
    """贪心合并回源区间。

    按起始偏移排序后逐个并入当前块，仅当：
      - 与当前块的间隙 <= max_gap，且
      - 合并后的并集长度 <= fetch_limit
    时才合并；否则另起一个回源块。任何结果块都不会超过 fetch_limit。

    返回 (回源块列表, 每个原范围所属块的编号列表)。
    """
    order = sorted(range(len(normalized)), key=lambda i: normalized[i])
    blocks: list[list[int]] = []  # 每块是 (start, end)
    owner: list[int] = [0] * len(normalized)

    cur_start = cur_end = -1
    cur_index = -1

    for i in order:
        start, end = normalized[i]
        if cur_start == -1:
            cur_start, cur_end, cur_index = start, end, 0
            blocks.append([cur_start, cur_end])
            owner[i] = cur_index
            continue

        gap = start - cur_end - 1
        merged_length = max(cur_end, end) - cur_start + 1
        if gap <= max_gap and merged_length <= fetch_limit:
            # 落入或紧邻当前块：合并（包含关系时块不变）。
            if end > cur_end:
                cur_end = end
                blocks[cur_index][1] = end
        else:
            # 间隙过大或合并后超过上限：另起一个回源块。
            cur_start, cur_end = start, end
            cur_index = len(blocks)
            blocks.append([cur_start, cur_end])
        owner[i] = cur_index

    return [(s, e) for s, e in blocks], owner


@app.post("/plan", response_model=PlanResponse)
def plan(req: PlanRequest) -> PlanResponse:
    normalized = normalize(req)
    block_tuples, owner = build_plan(normalized, req.max_gap, req.fetch_limit)

    fetch_blocks = [
        FetchBlock(
            index=i,
            start=s,
            end=e,
            length=e - s + 1,
            range_header=f"bytes={s}-{e}",
        )
        for i, (s, e) in enumerate(block_tuples)
    ]

    mappings: list[RangeMapping] = []
    for i, (start, end) in enumerate(normalized):
        block_index = owner[i]
        bstart, bend = block_tuples[block_index]
        # 排序合并保证该范围完整包含在其所属块中，映射只有一个切片。
        slices = [
            SliceOut(
                block_index=block_index,
                block_start=bstart,
                block_end=bend,
                offset_in_block=start - bstart,
                length=end - start + 1,
            )
        ]
        mappings.append(
            RangeMapping(
                index=i,
                type=req.ranges[i].type,
                start=start,
                end=end,
                slices=slices,
            )
        )

    return PlanResponse(
        object_length=req.object_length,
        blocks=fetch_blocks,
        mappings=mappings,
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
