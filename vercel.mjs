export const config = {
  version: 2,
  rewrites: [{
    source: '/:path*',
    destination: 'https://learn2play-staging.onrender.com/:path*',
  }],
};
