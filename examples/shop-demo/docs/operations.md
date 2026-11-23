# Operations

## Run it in a container

The container listens on port 9000 and the API is then reachable at http://localhost:8000.

```bash
docker compose up api queue
docker compose logs -f db
```

## Deploy it

```bash
helm install shop ./deploy/chart --set image.tags=1.4 --set replicaCount=3
terraform apply -var region=us-east-1 -var 'instance_size=small'
```
