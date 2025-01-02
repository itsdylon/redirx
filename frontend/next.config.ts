/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'export',
  images: {
    unoptimized: true,
  },
  basePath: '/redirx',
  assetPrefix: '/redirx/',
  trailingSlash: true,
}

module.exports = nextConfig