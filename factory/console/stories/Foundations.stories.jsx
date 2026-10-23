import { TokenCatalog, IconCatalog } from './Catalog.jsx';

export default { title: 'Foundations/Catalog', component: TokenCatalog, subcomponents: { IconCatalog } };
export const Tokens = {};
export const Icons = { render: () => <IconCatalog /> };
