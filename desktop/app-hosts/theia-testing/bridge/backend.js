// An ephemeral Electron session supplies this header only to its own loopback origin.
import { ContainerModule } from '@theia/core/shared/inversify';
import {
  BackendApplicationContribution,
  EarlyExpressMiddleware,
} from '@theia/core/lib/node/backend-application.js';
import { WsRequestValidatorContribution } from '@theia/core/lib/node/ws-request-validators.js';
const secret = process.env.MODOT_HOST_SESSION;
if (!secret || secret.length !== 64) throw Error('Missing private app-host session');
const accepts = (request) => request.headers['x-modot-host-session'] === secret;
export default new ContainerModule((bind) => {
  bind(BackendApplicationContribution).toDynamicValue(({ container }) => ({
    initialize() {
      container
        .get(EarlyExpressMiddleware)
        .handlers.unshift((request, response, next) =>
          accepts(request) ? next() : response.sendStatus(403),
        );
    },
  }));
  bind(WsRequestValidatorContribution).toConstantValue({ allowWsUpgrade: accepts });
});
