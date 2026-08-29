declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        namespace messages {
            let altTextRequired: string;
        }
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: [];
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "altTextRequired";
    }>): {
        "image, imageReference"(node: Image | ImageReference): void;
        html(node: import("mdast").Html): void;
    };
}
export default _default;
export type RequireAltTextMessageIds = "altTextRequired";
export type RequireAltTextOptions = [];
export type RequireAltTextRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: RequireAltTextOptions;
    MessageIds: RequireAltTextMessageIds;
}>;
import type { Image } from "mdast";
import type { ImageReference } from "mdast";
import type { MarkdownRuleDefinition } from "../types.js";
