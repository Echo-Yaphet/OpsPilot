export namespace processor {
    export namespace meta {
        let name: string;
        let version: string;
    }
    export { preprocess };
    export { postprocess };
    export { SUPPORTS_AUTOFIX as supportsAutofix };
}
/**
 * Extracts lintable code blocks from Markdown text.
 * @param {string} sourceText The text of the file.
 * @param {string} filename The filename of the file.
 * @returns {Array<{ filename: string, text: string }>} Source code blocks to lint.
 */
declare function preprocess(sourceText: string, filename: string): Array<{
    filename: string;
    text: string;
}>;
/**
 * Transforms generated messages for output.
 * @param {Array<LintMessage[]>} messages An array containing one array of messages
 *     for each code block returned from `preprocess`.
 * @param {string} filename The filename of the file
 * @returns {LintMessage[]} A flattened array of messages with mapped locations.
 */
declare function postprocess(messages: Array<LintMessage[]>, filename: string): LintMessage[];
declare const SUPPORTS_AUTOFIX: true;
import type { LintMessage } from "@eslint/core";
export {};
