// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import docsNavigation from './docs-navigation.json' with { type: 'json' };

export default defineConfig({
  integrations: [
    starlight({
      title: 'Shiori',
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        'zh-cn': { label: '简体中文', lang: 'zh-CN' },
      },
      sidebar: docsNavigation.sidebar,
    }),
  ],
});
