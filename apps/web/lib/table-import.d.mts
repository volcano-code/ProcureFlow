import type {EvidenceRef,QuoteValues,TableImportSelection,TableImportSheet} from './api';
export type TableField = Exclude<keyof QuoteValues,'lines'>;
export const TABLE_FIELDS: [TableField,string][];
export const TABLE_HEADER_FIELDS: [TableField,string][];
export const TABLE_LINE_FIELDS: [TableField,string][];
export const TABLE_MAX_ROWS: number;
export interface ImportTicket {epoch:number;signal:AbortSignal}
export interface ImportScope {begin():ImportTicket|null;current(ticket:ImportTicket|null):boolean;finish(ticket:ImportTicket):void;dispose():void}
export function createImportScope():ImportScope;
export function importReadOnly(status:string):boolean;
export function importExpired(expiresAt:string,now?:number):boolean;
export function compactMapping(mapping:Partial<Record<keyof QuoteValues,string|null>>):Partial<Record<TableField,string>>;
export function selectedRows(selection:TableImportSelection):number[];
export function mappingProblems(selection:TableImportSelection,sheet:TableImportSheet|undefined):string[];
export function selectionForSheet(sheet:TableImportSheet,multiItem?:boolean):TableImportSelection;
export function previewBody(revision:number,selection:TableImportSelection):TableImportSelection&{expected_revision:number};
export interface TablePreviewSection {id:string;label:string|null;fields:{field:TableField;label:string;path:string;value:string|number|null|undefined}[]}
export function previewSections(values:QuoteValues):TablePreviewSection[];
export function previewSources(source:EvidenceRef|null|undefined):EvidenceRef[];
