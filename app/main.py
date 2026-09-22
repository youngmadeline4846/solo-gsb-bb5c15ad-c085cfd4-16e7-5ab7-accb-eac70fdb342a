"""HTTP Range 回源规划与组装服务。

POST /plan 接收对象总长度、若干闭区间或 suffix 范围、允许合并的最大间隙
以及单次回源上限，输出合并后的回源块，以及每个请求范围在回源块中的
切片映射，使重叠范围可以复用字节并按映射无歧义重组。

POST /assemble 接收 /plan 返回的 blocks、mappings 以及按块提供的 Base64
回源内容，校验块索引唯一、内容可解码且长度匹配、切片不越界后，依据切片
映射按原 ranges 顺序重组，返回每段的 Base64 内容与长度。
"""

from __future__ import annotations

import base64
import binascii
import re
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


# --------------------- /assemble 请求/响应模型 ---------------------


class BlockContent(BaseModel):
    """按块提供的回源内容（标准 Base64 编码）。"""

    block_index: Annotated[int, StrictInt] = Field(..., ge=0, description="回源块编号，对应 blocks[].index")
    data_base64: str = Field(..., min_length=1, description="该块回源到的原始字节，标准 Base64 编码")


class AssembleRequest(BaseModel):
    blocks: list[FetchBlock] = Field(..., min_length=1, description="/plan 返回的 blocks")
    mappings: list[RangeMapping] = Field(..., min_length=1, description="/plan 返回的 mappings")
    contents: list[BlockContent] = Field(..., min_length=1, description="按块提供的 Base64 回源内容")


class AssembledSegment(BaseModel):
    index: int = Field(..., description="该段在原请求 ranges 中的下标")
    start: int = Field(..., description="规范化后的起始字节偏移（含）")
    end: int = Field(..., description="规范化后的结束字节偏移（含）")
    length: int = Field(..., ge=1, description="重组后的字节数")
    data_base64: str = Field(..., description="重组后的内容，标准 Base64 编码")


class AssembleResponse(BaseModel):
    object_length: int
    segments: list[AssembledSegment]


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


class AssemblyError(Exception):
    """组装阶段的语义校验失败（缺块、长度不符、切片越界等），对应 HTTP 422。

    loc 为可定位错误的字段路径（与 pydantic 风格一致），例如
    ["contents", 2, "data_base64"] 或 ["mappings", 0, "slices", 0]。
    """

    def __init__(self, message: str, loc: list[str | int] | None = None):
        self.message = message
        self.loc = loc or []
        super().__init__(message)


@app.exception_handler(AssemblyError)
async def assembly_error_handler(request: Request, exc: AssemblyError):
    return JSONResponse(
        status_code=422,
        content={"error": "assembly_failed", "message": exc.message, "loc": exc.loc},
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


# ----------------------------- 核心组装逻辑 -----------------------------


_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


def decode_block_b64(item: BlockContent) -> bytes:
    """解码单块 Base64 内容，拒绝非标准字母表、错误填充等任何无效输入。"""
    raw = item.data_base64
    if len(raw) % 4 != 0 or not _BASE64_RE.match(raw):
        raise AssemblyError(
            "data_base64 必须是标准 Base64（字母表 A-Za-z0-9+/，长度为 4 的倍数，"
            "末尾至多 2 个 '=' 填充）",
            ["contents", item.block_index, "data_base64"],
        )
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AssemblyError(
            f"Base64 解码失败：{exc}",
            ["contents", item.block_index, "data_base64"],
        ) from exc


def collect_block_bytes(req: AssembleRequest) -> dict[int, bytes]:
    """校验 blocks 自洽性，并把 contents 关联为 {block_index: 原始字节}。

    拒绝：blocks 索引非 0..n-1 唯一连续、contents 缺块/重复/多余块、
    Base64 无效、解码后字节数与声明长度不符。
    """
    block_by_index: dict[int, FetchBlock] = {}
    for pos, block in enumerate(req.blocks):
        if block.start < 0 or block.end < 0:
            raise AssemblyError(
                f"块 {block.index} 的 start/end 不得为负", ["blocks", pos]
            )
        if block.index != pos:
            raise AssemblyError(
                f"blocks 必须按 index=0..{len(req.blocks) - 1} 唯一且连续排列，"
                f"位置 {pos} 的 index 为 {block.index}",
                ["blocks", pos, "index"],
            )
        if block.length != block.end - block.start + 1 or block.length <= 0:
            raise AssemblyError(
                f"块 {block.index} 描述不自洽：length={block.length}，"
                f"按 [{block.start},{block.end}] 应为 {block.end - block.start + 1}",
                ["blocks", pos],
            )
        if block.range_header != f"bytes={block.start}-{block.end}":
            raise AssemblyError(
                f"块 {block.index} 的 range_header 与 start/end 不一致",
                ["blocks", pos, "range_header"],
            )
        block_by_index[block.index] = block

    seen: set[int] = set()
    raw_by_index: dict[int, bytes] = {}
    for pos, item in enumerate(req.contents):
        if item.block_index in seen:
            raise AssemblyError(
                f"块 {item.block_index} 的回源内容重复提供",
                ["contents", pos, "block_index"],
            )
        seen.add(item.block_index)

        block = block_by_index.get(item.block_index)
        if block is None:
            raise AssemblyError(
                f"提供了 blocks 中不存在的多余块 {item.block_index}",
                ["contents", pos, "block_index"],
            )

        data = decode_block_b64(item)
        if len(data) != block.length:
            raise AssemblyError(
                f"块 {item.block_index} 解码后字节数为 {len(data)}，"
                f"与声明长度 {block.length} 不符",
                ["contents", pos, "data_base64"],
            )
        raw_by_index[item.block_index] = data

    missing = sorted(set(block_by_index) - seen)
    if missing:
        raise AssemblyError(
            f"缺少以下回源块的内容：{missing}",
            ["contents"],
        )
    return raw_by_index


def assemble(req: AssembleRequest) -> AssembleResponse:
    """校验映射与切片并按原 ranges 顺序重组各段字节。"""
    raw_by_index = collect_block_bytes(req)

    assembled_by_index: dict[int, bytes] = {}
    mapping_indexes: set[int] = set()
    for pos, mapping in enumerate(req.mappings):
        loc = ["mappings", pos]
        if mapping.index < 0:
            raise AssemblyError("mapping 的 index 不得为负", loc + ["index"])
        if mapping.index in mapping_indexes:
            raise AssemblyError(f"mapping 索引 {mapping.index} 重复", loc + ["index"])
        mapping_indexes.add(mapping.index)

        if mapping.start < 0 or mapping.end < 0:
            raise AssemblyError("start/end 不得为负", loc)
        if mapping.end < mapping.start:
            raise AssemblyError("end 不得小于 start", loc)
        declared_length = mapping.end - mapping.start + 1

        assembled = bytearray()
        for s_pos, slc in enumerate(mapping.slices):
            sloc = loc + ["slices", s_pos]
            block = next((b for b in req.blocks if b.index == slc.block_index), None)
            if block is None:
                raise AssemblyError(
                    f"切片引用了不存在的块 {slc.block_index}", sloc + ["block_index"]
                )
            if slc.block_start != block.start or slc.block_end != block.end:
                raise AssemblyError(
                    f"切片记录的块边界 [{slc.block_start},{slc.block_end}] "
                    f"与块 {block.index} 的 [{block.start},{block.end}] 不一致",
                    sloc,
                )
            if slc.length < 1:
                raise AssemblyError("切片 length 必须 >= 1", sloc + ["length"])
            if slc.offset_in_block < 0:
                raise AssemblyError(
                    "offset_in_block 不得为负", sloc + ["offset_in_block"]
                )
            if slc.offset_in_block + slc.length > block.length:
                raise AssemblyError(
                    f"切片越界：offset_in_block={slc.offset_in_block}、"
                    f"length={slc.length} 超出块 {block.index} 的长度 {block.length}",
                    sloc,
                )

            start_pos = slc.offset_in_block
            assembled += raw_by_index[block.index][start_pos : start_pos + slc.length]

        if len(assembled) != declared_length:
            raise AssemblyError(
                f"组装长度 {len(assembled)} 与范围声明长度 {declared_length} 不符",
                loc,
            )
        assembled_by_index[mapping.index] = bytes(assembled)

    if mapping_indexes != set(range(len(req.mappings))):
        raise AssemblyError(
            f"mappings 的 index 必须恰好为 0..{len(req.mappings) - 1} 的唯一排列，"
            f"实际为 {sorted(mapping_indexes)}",
            ["mappings"],
        )

    segments = [
        AssembledSegment(
            index=mapping.index,
            start=mapping.start,
            end=mapping.end,
            length=mapping.end - mapping.start + 1,
            data_base64=base64.b64encode(assembled_by_index[mapping.index]).decode(
                "ascii"
            ),
        )
        for mapping in sorted(req.mappings, key=lambda m: m.index)
    ]

    object_length = max(
        (max(req.blocks, key=lambda b: b.end).end + 1) if req.blocks else 0,
        max((m.end + 1 for m in req.mappings), default=0),
    )
    return AssembleResponse(object_length=object_length, segments=segments)


@app.post("/assemble", response_model=AssembleResponse)
def assemble_endpoint(req: AssembleRequest) -> AssembleResponse:
    return assemble(req)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
