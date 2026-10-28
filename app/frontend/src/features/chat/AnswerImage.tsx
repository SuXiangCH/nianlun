import { useState } from "react";
import { ImageOff } from "lucide-react";
import type { AnswerImageAttachment } from "../../types";
import { answerImageUrl } from "../../api/client";

export function AnswerImage({ image, anchorId, pending = false, onOpen, onCitationClick }: {
  image?: AnswerImageAttachment;
  anchorId: string;
  pending?: boolean;
  onOpen: () => void;
  onCitationClick?: (citation: number) => void;
}) {
  const [failed, setFailed] = useState(false);
  const url = image?.status === "available" ? answerImageUrl(image.url) : null;
  if (pending || !image || !url || failed) return (
    <span className="answer-image-unavailable" role="status"><ImageOff size={15} aria-hidden="true" />{pending ? "图片处理中" : "图片暂不可用"}</span>
  );
  const rotated = image.orientation >= 5 && image.orientation <= 8;
  const width = rotated ? image.height : image.width;
  const height = rotated ? image.width : image.height;
  return <span className="answer-image" id={anchorId}>
    <button type="button" className="answer-image-open" title="放大图片" aria-label={`放大图片：${image.caption}`} onClick={onOpen}>
      <span className="answer-image-preview" style={width && height ? { aspectRatio: `${width} / ${height}` } : undefined}>
        <img src={url} alt={image.caption || "原文图片"} loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)} />
      </span>
    </button>
    <span className="answer-image-caption">{image.caption || "原文图片"}{image.citation_ids.map((citation) =>
      <button type="button" className="answer-image-citation" key={citation} title={`查看来源 [${citation}]`} onClick={() => onCitationClick?.(citation)}>[{citation}]</button>
    )}</span>
  </span>;
}
