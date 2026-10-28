import type { AnswerImageAttachment } from "../../types";
import { answerImageUrl } from "../../api/client";
import { ImageViewer as SharedImageViewer } from "../../components/ImageViewer";

export function ImageViewer({ images, selectedId, onClose }: {
  images: AnswerImageAttachment[]; selectedId: string; onClose: () => void;
}) {
  const available = images.flatMap((image) => {
    const url = image.status === "available" ? answerImageUrl(image.url) : null;
    return url ? [{ id: image.id, caption: image.caption, url }] : [];
  });
  return <SharedImageViewer images={available} selectedId={selectedId} onClose={onClose} />;
}
