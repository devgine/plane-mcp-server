import fs from "node:fs";
import { JSDOM } from "jsdom";
import * as Y from "yjs";
import { DOMParser as PMDOMParser, Schema } from "prosemirror-model";
import { prosemirrorJSONToYDoc } from "y-prosemirror";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const binary = Buffer.from(input.description_binary, "base64");
const html = String(input.description_html ?? "<p></p>");

// Schema covering the standard structures used by MCP-generated technical
// documentation. Unknown HTML is reduced to its supported semantic content
// rather than being injected into Yjs as opaque data.
const schema = new Schema({
  nodes: {
    doc: { content: "block+" },
    paragraph: {
      content: "inline*",
      group: "block",
      parseDOM: [{ tag: "p" }],
      toDOM() { return ["p", 0]; },
    },
    blockquote: {
      content: "block+",
      group: "block",
      defining: true,
      parseDOM: [{ tag: "blockquote" }],
      toDOM() { return ["blockquote", 0]; },
    },
    horizontal_rule: {
      group: "block",
      parseDOM: [{ tag: "hr" }],
      toDOM() { return ["hr"]; },
    },
    heading: {
      attrs: { level: { default: 1 } },
      content: "inline*",
      group: "block",
      defining: true,
      parseDOM: [1, 2, 3, 4, 5, 6].map((level) => ({ tag: `h${level}`, attrs: { level } })),
      toDOM(node) { return [`h${node.attrs.level}`, 0]; },
    },
    code_block: {
      content: "text*",
      marks: "",
      group: "block",
      code: true,
      defining: true,
      parseDOM: [{ tag: "pre", preserveWhitespace: "full" }],
      toDOM() { return ["pre", ["code", 0]]; },
    },
    text: { group: "inline" },
    image: {
      inline: true,
      attrs: { src: {}, alt: { default: null }, title: { default: null } },
      group: "inline",
      draggable: true,
      parseDOM: [{
        tag: "img[src]",
        getAttrs(dom) {
          return {
            src: dom.getAttribute("src"),
            alt: dom.getAttribute("alt"),
            title: dom.getAttribute("title"),
          };
        },
      }],
      toDOM(node) { return ["img", node.attrs]; },
    },
    hard_break: {
      inline: true,
      group: "inline",
      selectable: false,
      parseDOM: [{ tag: "br" }],
      toDOM() { return ["br"]; },
    },
    bullet_list: {
      content: "list_item+",
      group: "block",
      parseDOM: [{ tag: "ul" }],
      toDOM() { return ["ul", 0]; },
    },
    ordered_list: {
      attrs: { order: { default: 1 } },
      content: "list_item+",
      group: "block",
      parseDOM: [{
        tag: "ol",
        getAttrs(dom) { return { order: dom.hasAttribute("start") ? +dom.getAttribute("start") : 1 }; },
      }],
      toDOM(node) { return node.attrs.order === 1 ? ["ol", 0] : ["ol", { start: node.attrs.order }, 0]; },
    },
    list_item: {
      content: "paragraph block*",
      defining: true,
      parseDOM: [{ tag: "li" }],
      toDOM() { return ["li", 0]; },
    },
  },
  marks: {
    link: {
      attrs: { href: {}, title: { default: null } },
      inclusive: false,
      parseDOM: [{
        tag: "a[href]",
        getAttrs(dom) { return { href: dom.getAttribute("href"), title: dom.getAttribute("title") }; },
      }],
      toDOM(node) { return ["a", node.attrs, 0]; },
    },
    em: {
      parseDOM: [{ tag: "i" }, { tag: "em" }, { style: "font-style=italic" }],
      toDOM() { return ["em", 0]; },
    },
    strong: {
      parseDOM: [
        { tag: "strong" },
        { tag: "b", getAttrs: (node) => node.style.fontWeight !== "normal" && null },
        { style: "font-weight", getAttrs: (value) => /^(bold(er)?|[5-9]\d{2,})$/.test(value) && null },
      ],
      toDOM() { return ["strong", 0]; },
    },
    code: {
      parseDOM: [{ tag: "code" }],
      toDOM() { return ["code", 0]; },
    },
  },
});

const ydoc = new Y.Doc();
Y.applyUpdate(ydoc, new Uint8Array(binary));

const dom = new JSDOM(`<body>${html}</body>`);
const parsed = PMDOMParser.fromSchema(schema).parse(dom.window.document.body);
const bodyDoc = prosemirrorJSONToYDoc(schema, parsed.toJSON(), "default");

const bodyFragment = ydoc.getXmlFragment("default");
if (bodyFragment.length > 0) {
  bodyFragment.delete(0, bodyFragment.length);
}
Y.applyUpdate(ydoc, Y.encodeStateAsUpdate(bodyDoc));

process.stdout.write(Buffer.from(Y.encodeStateAsUpdate(ydoc)).toString("base64"));
