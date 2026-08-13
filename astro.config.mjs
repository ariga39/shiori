// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  integrations: [
    starlight({
      title: 'Shiori',
      defaultLocale: 'root',
      locales: {
        root: { label: 'English', lang: 'en' },
        'zh-cn': { label: '简体中文', lang: 'zh-CN' },
      },
      sidebar: [
        {
          label: 'Start',
          translations: { 'zh-CN': '开始' },
          items: ['index', 'getting-started'],
        },
        {
          label: 'User guide',
          translations: { 'zh-CN': '用户指南' },
          items: ['CONFIGURATION', 'privacy-policy', 'cli-mcp-reference'],
        },
        {
          label: 'Project',
          translations: { 'zh-CN': '项目' },
          items: [
            'DESIGN',
            'contributing',
            'adr/0001-atomic-rebuild-on-partial-embed-failure',
            'RELEASE_CHECKLIST',
          ],
        },
      ],
    }),
  ],
});
