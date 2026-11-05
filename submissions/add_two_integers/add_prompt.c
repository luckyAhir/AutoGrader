#include <stdio.h>

int main()
{
    long long first, second;

    printf("Enter two numbers: ");
    scanf("%lld %lld", &first, &second);
    printf("Sum = %lld\n", first + second);
    return 0;
}
