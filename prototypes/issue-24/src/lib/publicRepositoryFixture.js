// Design-only data. Production will render these from repository bootstrap and reprepro.
export const publicRepositoryFixture = {
  baseUrl: 'https://repo.probatou.com',
  suite: 'Luminous',
  component: 'main',
  architectures: ['amd64'],
  fingerprint: null,
  packages: [
    {name:'zoraxy',version:'3.1.4-1'},
    {name:'pocket-id',version:'1.8.2-1'},
    {name:'stalwart',version:'0.16.20-2'},
    {name:'debbuilder',version:'1.0.0'},
    {name:'seerr',version:'1.9.0-1'},
  ],
};
