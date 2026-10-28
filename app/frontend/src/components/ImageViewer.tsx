import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, ArrowRight, ExternalLink } from "lucide-react";
import { Modal } from "./Modal";

export interface ViewerImage {
  id: string;
  caption: string;
  url: string;
}

export function ImageViewer({ images, selectedId, onClose }: {
  images: ViewerImage[]; selectedId: string; onClose: () => void;
}) {
  const [index, setIndex] = useState(Math.max(0, images.findIndex((image) => image.id === selectedId)));
  const [failed, setFailed] = useState(false);
  const image = images[index];
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft") { event.preventDefault(); setIndex((current) => Math.max(0, current - 1)); setFailed(false); }
      if (event.key === "ArrowRight") { event.preventDefault(); setIndex((current) => Math.min(images.length - 1, current + 1)); setFailed(false); }
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [images.length]);
  if (!image) return null;
  return createPortal(<Modal title={image.caption || "原文图片"} description="" onClose={onClose}>
    <div className="answer-image-viewer">
      <div className="answer-image-viewer-stage">{failed ? <span role="status">图片暂不可用</span> : <img key={image.id} src={image.url} alt={image.caption || "原文图片"} referrerPolicy="no-referrer" onError={() => setFailed(true)} />}</div>
      <div className="answer-image-viewer-toolbar">
        <button type="button" className="icon-button" title="上一张" aria-label="上一张" disabled={index === 0} onClick={() => { setIndex(index - 1); setFailed(false); }}><ArrowLeft size={18} /></button>
        <span>{index + 1} / {images.length}</span>
        <button type="button" className="icon-button" title="下一张" aria-label="下一张" disabled={index === images.length - 1} onClick={() => { setIndex(index + 1); setFailed(false); }}><ArrowRight size={18} /></button>
        <a href={image.url} className="icon-button" title="查看原图" aria-label="查看原图" target="_blank" rel="noopener noreferrer"><ExternalLink size={18} /></a>
      </div>
    </div>
  </Modal>, document.body);
}
