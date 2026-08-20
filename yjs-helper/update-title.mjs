import fs from "node:fs";
import * as Y from "yjs";
import { Schema } from "prosemirror-model";
import { prosemirrorJSONToYDoc } from "y-prosemirror";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const binary = Buffer.from(input.description_binary, "base64");
const title = String(input.title ?? "");

const schema = new Schema({
  nodes: {
    doc: { content: "block+" },
    heading: {
      attrs: { level: { default: 1 } },
      content: "inline*",
      group: "block",
      defining: true,
      toDOM(node) { return [`h${node.attrs.level}`, 0]; },
      parseDOM: [{ tag: "h1", attrs: { level: 1 } }],
    },
    text: { group: "inline" },
  },
  marks: {},
});

const doc = new Y.Doc();
Y.applyUpdate(doc, new Uint8Array(binary));

const titleFragment = doc.getXmlFragment("title");
if (titleFragment.length > 0) {
  titleFragment.delete(0, titleFragment.length);
}

const titleJson = {
  type: "doc",
  content: [
    {
      type: "heading",
      attrs: { level: 1 },
      ...(title ? { content: [{ type: "text", text: title }] } : {}),
    },
  ],
};

const titleDoc = prosemirrorJSONToYDoc(schema, titleJson, "title");
Y.applyUpdate(doc, Y.encodeStateAsUpdate(titleDoc));

process.stdout.write(Buffer.from(Y.encodeStateAsUpdate(doc)).toString("base64"));
