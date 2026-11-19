# Enable writes safely

Let the assistant create, change or delete records in AFAS without giving it more power than the task needs.

## Before you start

- A read-only deployment works (see [Getting started](../tutorials/getting-started.md)).
- You have an AFAS **test** environment to rehearse in.

Writes are a property of the deployment, not of a user: `AFAS_ALLOW_WRITES=true` registers the write tools for
everyone who can log in. What each user can actually change is still decided by AFAS.

## Steps

1. Create a separate OAuth app connector in AFAS for writing. Link only the UpdateConnectors the assistant needs,
   and an authorisation group with only the people who should write through an assistant.

2. Deploy a second server instance for that app connector, on its own hostname and with its own volume and
   encryption key, pointed at the test environment:

   ```bash
   AFAS_ENVIRONMENT=test
   AFAS_ALLOW_WRITES=true
   AFAS_PUBLIC_URL=https://afas-mcp-write.example.com
   ```

   `afas-mcp-server --check` reports `"writes_enabled": true` for these settings.

3. Add this instance to your client under a distinct name, for example `afas-write`, so the assistant and you
   can tell the two apart.

4. Let the assistant rehearse without sending: ask it to call `afas_describe_update_connector` and then
   `afas_validate_payload` for the change. The result lists every schema problem and the exact JSON body that would
   go to AFAS.

5. Only then ask for the change. `afas_insert` and `afas_update` validate again before sending unless the call sets
   `validate: false`; `afas_delete` needs the key field and value and is irreversible.

6. Verify in AFAS, then repeat against production with an app connector in the production environment.

## What the assistant is told

With writes enabled the server's instructions ask the model to describe the connector, validate the payload, show
the user what will change, and confirm deletions before acting. The write tools are also annotated as not read-only
and, for update and delete, as destructive, so clients that ask for confirmation on such tools will do so.

These are instructions to a model, not guarantees. The limits that hold are the UpdateConnectors linked to the app
connector and the rights of the logged-in user in AFAS.

## See also

- [Tools](../reference/tools.md) for the exact payload shape of `element`.
- [Configuration](../reference/configuration.md) for every variable used above.
