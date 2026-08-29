declare namespace _default {
    namespace meta {
        let type: "problem";
        namespace docs {
            let recommended: boolean;
            let description: string;
            let url: string;
        }
        let fixable: "whitespace";
        namespace messages {
            let missingSpace: string;
        }
        let schema: {
            type: "object";
            properties: {
                checkClosedHeadings: {
                    type: "boolean";
                };
            };
            additionalProperties: false;
        }[];
        let defaultOptions: [{
            checkClosedHeadings: false;
        }];
    }
    function create(context: import("@eslint/core").RuleContext<{
        LangOptions: import("../types.js").MarkdownLanguageOptions;
        Code: import("../index.js").MarkdownSourceCode;
        RuleOptions: NoMissingAtxHeadingSpaceOptions;
        Node: import("mdast").Node | import("../language/markdown-source-code.js").InlineConfigComment;
        MessageIds: "missingSpace";
    }>): {
        heading(node: import("mdast").Heading): void;
        paragraph(node: import("mdast").Paragraph): void;
    };
}
export default _default;
export type NoMissingAtxHeadingSpaceMessageIds = "missingSpace";
export type NoMissingAtxHeadingSpaceOptions = [{
    checkClosedHeadings?: boolean;
}];
export type NoMissingAtxHeadingSpaceRuleDefinition = MarkdownRuleDefinition<{
    RuleOptions: NoMissingAtxHeadingSpaceOptions;
    MessageIds: NoMissingAtxHeadingSpaceMessageIds;
}>;
import type { MarkdownRuleDefinition } from "../types.js";
