from pydantic import BaseModel


class ReviewUnavailableRead(BaseModel):
    detail: str = "本期暂不提供报告复核写入。"
