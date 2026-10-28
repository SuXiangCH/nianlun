import { createContext, useContext, useState, type ComponentProps } from "react";
import ReactMarkdown, { type ExtraProps } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";
import type { AnswerImageAttachment } from "../../types";
import { normalizeHtmlTables, remarkAnswerImages, remarkOmitImages, imageSafeStreamingText, type MarkdownAstNode } from "./markdown";
import { AnswerImage } from "./AnswerImage";
import { ImageViewer } from "./ImageViewer";

const remarkCitationLinks = () => (tree: MarkdownAstNode): void => {
  const visit = (node: MarkdownAstNode, protectedNode = false): void => {
    if (!node.children) return;
    const nextChildren: MarkdownAstNode[] = [];
    const protectsChildren = protectedNode || node.type === "link" || node.type === "code" || node.type === "inlineCode";
    for (const child of node.children) {
      if (child.type !== "text" || protectsChildren || !child.value) { visit(child, protectsChildren); nextChildren.push(child); continue; }
      const pattern = /\[(\d+)\]/g;
      let cursor = 0;
      let match: RegExpExecArray | null;
      while ((match = pattern.exec(child.value)) !== null) {
        if (match.index > cursor) nextChildren.push({ type: "text", value: child.value.slice(cursor, match.index) });
        const citationId = Number(match[1]);
        nextChildren.push({ type: "link", url: `#citation-${citationId}`, data: { hProperties: { className: ["citation-link"], "data-citation-id": citationId, title: `查看来源 [${citationId}]` } }, children: [{ type: "text", value: match[0] }] });
        cursor = pattern.lastIndex;
      }
      if (cursor === 0) nextChildren.push(child);
      else if (cursor < child.value.length) nextChildren.push({ type: "text", value: child.value.slice(cursor) });
    }
    node.children = nextChildren;
  };
  visit(tree);
};

interface ImageContextValue {
  images: AnswerImageAttachment[];
  messageId: string;
  pending: boolean;
  onOpen: (imageId: string) => void;
  onCitationClick?: (id: number) => void;
}
const ImageContext = createContext<ImageContextValue>({ images: [], messageId: "", pending: false, onOpen: () => undefined });
const anchorFor = (messageId: string, imageId: string) => `answer-image-${encodeURIComponent(messageId)}-${encodeURIComponent(imageId)}`;

function ImageSpan({ node, children, ...props }: ComponentProps<"span"> & ExtraProps) {
  const context = useContext(ImageContext);
  if (!node || !("data-answer-image-id" in node.properties)) return <span {...props}>{children}</span>;
  const id = String(node.properties["data-answer-image-id"] || "");
  const image = context.images.find((item) => item.id === id && item.display_in_answer);
  const anchorId = anchorFor(context.messageId, id);
  if (image && !context.pending && node.properties["data-answer-image-repeat"] === "true") return (
    <a className="answer-image-repeat" href={`#${anchorId}`} onClick={(event) => { event.preventDefault(); document.getElementById(anchorId)?.scrollIntoView?.({ block: "center", behavior: "smooth" }); }}>查看前图</a>
  );
  return <AnswerImage image={image} anchorId={anchorId} pending={context.pending} onOpen={() => context.onOpen(id)} onCitationClick={context.onCitationClick} />;
}
function UntrustedImage() {
  const { pending } = useContext(ImageContext);
  return <AnswerImage anchorId="" pending={pending} onOpen={() => undefined} />;
}
const components = { span: ImageSpan, img: UntrustedImage };

export function ImagePreviewNotice({ count = 0 }: { count?: number }) {
  return count > 0 ? <p className="image-preview-notice" role="status">{count} 张来源图片未能预览</p> : null;
}

export function AnswerMarkdown({ text, className, onCitationClick, images = [], messageId = "", pending = false, omitImages = false }: {
  text: string; className: string; onCitationClick?: (citationId: number) => void;
  images?: AnswerImageAttachment[]; messageId?: string; pending?: boolean;
  omitImages?: boolean;
}) {
  const [selectedImage, setSelectedImage] = useState<string | null>(null);
  return <ImageContext.Provider value={{ images, messageId, pending, onOpen: setSelectedImage, onCitationClick }}>
    <div className={className} onClick={(event) => {
      if (!onCitationClick || !(event.target instanceof Element)) return;
      const link = event.target.closest<HTMLAnchorElement>("a.citation-link");
      if (!link) return;
      const citationId = Number(link.dataset.citationId);
      if (!Number.isInteger(citationId) || citationId <= 0) return;
      event.preventDefault(); onCitationClick(citationId);
    }}>
      <ReactMarkdown components={components} remarkPlugins={[remarkGfm, remarkMath, ...(onCitationClick ? [remarkCitationLinks] : []), omitImages ? remarkOmitImages : remarkAnswerImages]} rehypePlugins={[rehypeKatex]}>{normalizeHtmlTables(pending ? imageSafeStreamingText(text) : text)}</ReactMarkdown>
    </div>
    {selectedImage && <ImageViewer images={images.filter((item) => item.display_in_answer)} selectedId={selectedImage} onClose={() => setSelectedImage(null)} />}
  </ImageContext.Provider>;
}

export function SourceImages({ images, messageId, onCitationClick }: { images: AnswerImageAttachment[]; messageId: string; onCitationClick?: (id: number) => void }) {
  const [selected, setSelected] = useState<string | null>(null);
  if (!images.length) return null;
  return <div className="source-images">
    {images.map((image) => <AnswerImage key={image.id} image={image} anchorId={`source-${anchorFor(messageId, image.id)}`} onOpen={() => setSelected(image.id)} onCitationClick={onCitationClick} />)}
    {selected && <ImageViewer images={images} selectedId={selected} onClose={() => setSelected(null)} />}
  </div>;
}
