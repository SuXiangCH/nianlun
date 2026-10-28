import { useState } from "react";
import { ImageOff, LoaderCircle } from "lucide-react";
import type { DocumentPreviewImage } from "../../types";
import { documentImageUrl } from "../../api/client";

export function DocumentImage({ image, caption, onOpen }: {
  image?: DocumentPreviewImage; caption: string; onOpen: () => void;
}) {
  const [state, setState] = useState<"loading" | "loaded" | "failed">("loading");
  const url = image?.status === "available" ? documentImageUrl(image.url) : null;
  if (!image || !url || state === "failed") return (
    <span className="document-image-unavailable" role="status"><ImageOff size={17} aria-hidden="true" /><span>{caption && <strong>{caption}</strong>}图片暂不可用</span></span>
  );
  const rotated = image.orientation >= 5 && image.orientation <= 8;
  const width = rotated ? image.height : image.width;
  const height = rotated ? image.width : image.height;
  return <span className="document-image">
    <button type="button" className="document-image-open" title="放大图片" aria-label={`放大图片：${caption || "原文图片"}`} onClick={onOpen}>
      <span className="document-image-frame" style={{ aspectRatio: width && height ? `${width} / ${height}` : "4 / 3" }}>
        {state === "loading" && <span className="document-image-loading" role="status"><LoaderCircle className="knowledge-spinning" size={18} aria-hidden="true" />图片加载中</span>}
        <img src={url} alt={caption || "原文图片"} loading="lazy" decoding="async" referrerPolicy="no-referrer" onLoad={() => setState("loaded")} onError={() => setState("failed")} />
      </span>
    </button>
    {caption && <span className="document-image-caption">{caption}</span>}
  </span>;
}
